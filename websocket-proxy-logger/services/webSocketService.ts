
import {
  WebSocketProxyStatus,
  WSServerSentMessage,
  WSClientSentMessage,
  WSHttpRequestMessage,
  WSHttpResponseMessage,
  WSStreamStartMessage,
  WSStreamChunkMessage,
  WSStreamEndMessage,
  WSErrorMessage,
  WSPingMessage
} from '../types';
import { WEBSOCKET_PROXY_URL } from '../config'; // Import from new config file

const BASE_WEBSOCKET_URL = WEBSOCKET_PROXY_URL; // Use imported constant
const PING_INTERVAL_MS = 25 * 1000; // 25 seconds
const RECONNECT_INITIAL_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 30 * 1000;
const RECONNECT_JITTER_MS = 500;


// Numeric constants for WebSocket readyState
const WS_CONNECTING = 0;
const WS_OPEN = 1;
const WS_CLOSING = 2;
const WS_CLOSED = 3;

let socket: WebSocket | null = null;
let currentStatus: WebSocketProxyStatus = WebSocketProxyStatus.IDLE;
let onStatusChangeCallback: ((status: WebSocketProxyStatus, details?: string) => void) | null = null;
let pingIntervalId: number | null = null;
let reconnectTimeoutId: number | null = null;
let currentReconnectDelay = RECONNECT_INITIAL_DELAY_MS;
let explicitClose = false;
let currentJwtToken: string | null = null;

function updateStatus(newStatus: WebSocketProxyStatus, details?: string) {
  if (currentStatus === newStatus && !details) return; 
  currentStatus = newStatus;
  if (onStatusChangeCallback) {
    onStatusChangeCallback(currentStatus, details);
  }
  console.log(`WebSocket Proxy Status: ${currentStatus}${details ? ` - ${details}` : ''}`);
}

function sendToServer(message: WSClientSentMessage) {
  if (socket && socket.readyState === WS_OPEN) {
    try {
      const messageString = JSON.stringify(message);
      socket.send(messageString);
      // console.log("WebSocket Proxy: Sent message", message);
    } catch (error) {
      console.error("WebSocket Proxy: Error serializing message for sending:", error, message);
    }
  } else {
    console.warn("WebSocket Proxy: Cannot send message, socket not open.", message);
  }
}

async function handleHttpRequest(request: WSHttpRequestMessage) {
  const { id, payload } = request;
  let { method, url, headers, body } = payload; 

  if (method === 'GET') {
    try {
      const parsedUrl = new URL(url);
      if (parsedUrl.pathname.endsWith('/v1beta/models') || parsedUrl.pathname.endsWith('/v1beta/models/')) {
        if (parsedUrl.searchParams.has('key')) {
          parsedUrl.searchParams.delete('key');
          url = parsedUrl.toString();
          console.log(`WebSocket Proxy: Modified URL for ${id} to remove 'key' param: ${url}`);
        }
      }
    } catch (e) {
      console.error(`WebSocket Proxy: Error parsing URL for modification for request ID ${id}: ${url}`, e);
    }
  }


  const fetchOptions: RequestInit = {
    method,
    headers,
  };



  if (method !== 'GET' && method !== 'HEAD') {
    if (body !== undefined && body !== null) {
        try {
            let parsedBody = typeof body === 'string' ? JSON.parse(body) : body;
            let modified = false;

            if (parsedBody.tools && Array.isArray(parsedBody.tools) && parsedBody.tools.length > 0) {
                const toolsString = JSON.stringify(parsedBody.tools);
                const hasServerSideTool = /(?:google[_]?search(?:_retrieval)?|google[_]?maps|url[_]?context|code[_]?execution|file[_]?search|retrieval|vertex[_]?ai[_]?search)/i.test(toolsString);

                if (hasServerSideTool) {
                    console.log(`WebSocket Proxy: Detected Google Search tool for request ID ${id}`);

                    if (url.includes('gemini-2.5')) {
                        // 1. 隔离 Google Search (剔除客户端工具)
                        parsedBody.tools = [ { "googleSearch": {} } ];

                        // 2. 对于 Gemini 2.5，绝对不能开启 includeServerSideToolInvocations，
                        // 因为这会直接触发 'Tool call context circulation is not enabled' 报错。
                        // 如果 OpenWebUI 自己带了，也要强制删除
                        if (parsedBody.toolConfig && parsedBody.toolConfig.includeServerSideToolInvocations !== undefined) {
                            delete parsedBody.toolConfig.includeServerSideToolInvocations;
                            if (parsedBody.toolConfig.experimental) {
                                delete parsedBody.toolConfig.experimental;
                            }
                            if (Object.keys(parsedBody.toolConfig).length === 0) {
                                delete parsedBody.toolConfig;
                            }
                            console.log(`WebSocket Proxy: Removed toolConfig.includeServerSideToolInvocations for Gemini 2.5`);
                        }
                    } else {
                        // 对于未来的 Gemini 3.0 或其他模型，才开启这个选项以获得显式的工具调用反馈
                        parsedBody.toolConfig = parsedBody.toolConfig || {};
                        parsedBody.toolConfig.includeServerSideToolInvocations = true;
                    }

                    modified = true;
                }
            }

            // === Fix Gemini :countTokens 400 "Request contains an invalid argument" ===
            // The v1beta countTokens endpoint is strict: the body must be shaped as
            // { "contents": [...] } (or { "generateContentRequest": {...} }). It rejects
            // top-level "model" (already in the URL path) and "systemInstruction"
            // (often carrying a "role" field), returning INVALID_ARGUMENT.
            if (url.includes(':countTokens') || url.includes('/countTokens')) {
                let countChanged = false;

                // 1) Drop the redundant top-level "model" - it lives in the URL path.
                if (parsedBody.model !== undefined) {
                    delete parsedBody.model;
                    countChanged = true;
                }

                // 2) Fold systemInstruction into contents (as user text) so the token
                //    estimate still includes the system prompt, while the field the
                //    backend rejects is removed entirely.
                if (parsedBody.systemInstruction) {
                    const si: any = parsedBody.systemInstruction;
                    let parts: any[] = [];
                    if (typeof si === 'string') {
                        parts = [{ text: si }];
                    } else if (Array.isArray(si.parts)) {
                        parts = si.parts;
                    }
                    const systemText = parts
                        .map((p: any) => (typeof p === 'string' ? p : (p && p.text) || ''))
                        .filter((t: string) => typeof t === 'string' && t.trim())
                        .join('\n\n');
                    delete parsedBody.systemInstruction;
                    countChanged = true;
                    if (systemText.trim()) {
                        if (!Array.isArray(parsedBody.contents)) {
                            parsedBody.contents = [];
                        }
                        // Prepend as a user message so system tokens are still counted.
                        parsedBody.contents.unshift({ role: 'user', parts: [{ text: systemText }] });
                    }
                }

                // 3) Fallback only if the backend STILL rejects the request:
                //    uncomment the next line to also drop tools (undercounts tool-schema
                //    tokens but guarantees a valid body).
                // if (parsedBody.tools !== undefined) { delete parsedBody.tools; countChanged = true; }

                if (countChanged) {
                    console.log(`[REQ ${id}] 2c. Normalized :countTokens body for Gemini (dropped top-level model, folded systemInstruction into contents).`);
                    modified = true;
                }
            }
            // Convert thinkingLevel to uppercase to fix Invalid Argument error
            if (parsedBody.generationConfig?.thinkingConfig?.thinkingLevel) {
                const level = parsedBody.generationConfig.thinkingConfig.thinkingLevel;
                if (typeof level === 'string') {
                    console.log(`[REQ ${id}] 2b. Converting thinkingLevel '${level}' to uppercase.`);
                    parsedBody.generationConfig.thinkingConfig.thinkingLevel = level.toUpperCase();
                    modified = true;
                }
            }
            // === Apply generationConfig defaults only when the values are absent ===
            // maxOutputTokens: 32768, thinkingConfig.includeThoughts: true
            // Skipped for :countTokens (its body schema is strict; adding
            // generationConfig there would re-trigger the 400 INVALID_ARGUMENT).
            if (!url.includes(':countTokens') && !url.includes('/countTokens')) {
                let defaultsApplied = false;

                if (!parsedBody.generationConfig) {
                    parsedBody.generationConfig = {};
                }

                if (parsedBody.generationConfig.maxOutputTokens === undefined) {
                    parsedBody.generationConfig.maxOutputTokens = 32768;
                    defaultsApplied = true;
                }

                if (!parsedBody.generationConfig.thinkingConfig) {
                    parsedBody.generationConfig.thinkingConfig = {};
                }

                if (parsedBody.generationConfig.thinkingConfig.includeThoughts === undefined) {
                    parsedBody.generationConfig.thinkingConfig.includeThoughts = true;
                    defaultsApplied = true;
                }

                if (defaultsApplied) {
                    console.log(`[REQ ${id}] 2d. Applied generationConfig defaults (only missing fields): maxOutputTokens=32768, thinkingConfig.includeThoughts=true.`);
                    modified = true;
                }
            }
            if (modified) {
                const finalBodyStr = JSON.stringify(parsedBody);
                console.log(`WebSocket Proxy: Final modified request body:`, JSON.stringify(parsedBody, null, 2));
                fetchOptions.body = finalBodyStr;
            } else {
                fetchOptions.body = body;
                console.log(`WebSocket Proxy: Final modified request body:`, JSON.stringify(parsedBody, null, 2));
            }

        } catch (e) {
            console.error(`WebSocket Proxy: Error modifying request body for request ID ${id}`, e);
            fetchOptions.body = body; 
        }
    }
  }

  try {
    const response = await fetch(url, fetchOptions);

    const responseHeaders: Record<string, string> = {};
    response.headers.forEach((value, key) => {
      responseHeaders[key] = value;
    });

    if (response.body && typeof response.body.getReader === 'function') { 
      const streamStartMessage: WSStreamStartMessage = {
        id,
        type: "stream_start",
        payload: { status: response.status, headers: responseHeaders },
      };
      sendToServer(streamStartMessage);

      const reader = response.body.getReader();
      const decoder = new TextDecoder(); 

      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunkData = decoder.decode(value, { stream: true }); 
        const streamChunkMessage: WSStreamChunkMessage = {
          id,
          type: "stream_chunk",
          payload: { data: chunkData },
        };
        sendToServer(streamChunkMessage);
      }
      const finalChunk = decoder.decode();
      if (finalChunk) {
         const streamChunkMessage: WSStreamChunkMessage = {
          id,
          type: "stream_chunk",
          payload: { data: finalChunk },
        };
        sendToServer(streamChunkMessage);
      }


      const streamEndMessage: WSStreamEndMessage = {
        id,
        type: "stream_end",
        payload: {},
      };
      sendToServer(streamEndMessage);

    } else {
      const responseBodyText = await response.text();
      const httpResponseMessage: WSHttpResponseMessage = {
        id,
        type: "http_response",
        payload: {
          status: response.status,
          headers: responseHeaders,
          body: responseBodyText,
        },
      };
      sendToServer(httpResponseMessage);
    }
  } catch (error) {
    console.warn(`WebSocket Proxy: Fetch error for request ID ${id} (${method} ${url}):`, error);
    const errorMessage: WSErrorMessage = {
      id,
      type: "error",
      payload: {
        code: "FETCH_ERROR",
        message: error instanceof Error ? error.message : String(error),
      },
    };
    if (error instanceof Response && error.status) { 
        errorMessage.payload.code = "HTTP_ERROR";
        errorMessage.payload.http_response = {
            status: error.status,
            headers: {}, 
            body: await error.text().catch(() => "Could not read error body"),
        };
    }
    sendToServer(errorMessage);
  }
}


function onSocketOpen() {
  updateStatus(WebSocketProxyStatus.CONNECTED);
  currentReconnectDelay = RECONNECT_INITIAL_DELAY_MS; 
  if (reconnectTimeoutId) {
    clearTimeout(reconnectTimeoutId);
    reconnectTimeoutId = null;
  }
  startPing();
}

function onSocketMessage(event: MessageEvent) {
  try {
    const message = JSON.parse(event.data as string) as WSServerSentMessage;

    switch (message.type) {
      case "http_request":
        handleHttpRequest(message as WSHttpRequestMessage);
        break;
      case "pong":
        break;
      default:
        console.warn("WebSocket Proxy: Received unknown message type", message);
    }
  } catch (error) {
    console.error("WebSocket Proxy: Error parsing message from server or handling it:", error, event.data);
  }
}

function onSocketError(event: Event) {
  // Use console.warn instead of console.error to prevent test runner from failing
  // if the external websocket server is temporarily unreachable. The connection
  // will be retried automatically by the onSocketClose handler.
  console.warn("WebSocket Proxy: Socket error:", event);
}

function onSocketClose(event: CloseEvent) {
  stopPing();
  if (reconnectTimeoutId) { 
    return;
  }

  if (explicitClose) {
    updateStatus(WebSocketProxyStatus.IDLE, `Connection closed by client. Code: ${event.code}`);
    explicitClose = false; 
  } else {
    updateStatus(WebSocketProxyStatus.DISCONNECTED, `Connection closed. Code: ${event.code}, Reason: ${event.reason || 'N/A'}`);
    scheduleReconnect();
  }
  socket = null;
}

function startPing() {
  stopPing(); 
  pingIntervalId = window.setInterval(() => {
    const pingMsg: WSPingMessage = { type: "ping" };
    sendToServer(pingMsg);
  }, PING_INTERVAL_MS);
}

function stopPing() {
  if (pingIntervalId) {
    clearInterval(pingIntervalId);
    pingIntervalId = null;
  }
}

function scheduleReconnect() {
  if (explicitClose || !currentJwtToken) { 
    updateStatus(WebSocketProxyStatus.IDLE, "Reconnection not attempted (explicit close or no token).");
    return;
  }
  if (reconnectTimeoutId) {
    clearTimeout(reconnectTimeoutId); 
  }

  const delayWithJitter = currentReconnectDelay + Math.random() * RECONNECT_JITTER_MS;
  updateStatus(WebSocketProxyStatus.RECONNECTING, `Attempting to reconnect in ${Math.round(delayWithJitter / 1000)}s...`);

  reconnectTimeoutId = window.setTimeout(() => {
    reconnectTimeoutId = null; 
    if (currentJwtToken) { 
        connect(currentJwtToken);
    } else {
        updateStatus(WebSocketProxyStatus.IDLE, "Reconnect aborted: JWT token became unavailable.");
    }
  }, delayWithJitter);

  currentReconnectDelay = Math.min(currentReconnectDelay * 2, RECONNECT_MAX_DELAY_MS);
}


function connect(jwtToken: string) {
  if (!jwtToken) { // This check is still useful if connect is somehow called directly with a null/empty token
    updateStatus(WebSocketProxyStatus.ERROR, "JWT Token is required to connect.");
    return;
  }
  currentJwtToken = jwtToken; 

  if (socket && (socket.readyState === WS_OPEN || socket.readyState === WS_CONNECTING)) {
    console.log("WebSocket Proxy: Already connected or connecting.");
    return;
  }

  explicitClose = false;
  updateStatus(WebSocketProxyStatus.CONNECTING);

  // Optional stable provider name injected by the keep-alive supervisor via
  // Playwright add_init_script (window.__PROVIDER_NAME__). When the proxy server
  // supports the ?provider_name= parameter, this connection registers under that
  // name instead of a random aistudio-XXXX id, so restarts re-use the same provider
  // entry and usage stats stay attached to one identity. Servers without support
  // simply ignore the extra parameter.
  const injectedProviderName = (window as any).__PROVIDER_NAME__;
  const providerName = typeof injectedProviderName === 'string' ? injectedProviderName.trim() : '';
  
  let wsUrl = '';
  try {
    const urlObj = new URL(BASE_WEBSOCKET_URL);
    urlObj.searchParams.set('auth_token', jwtToken);
    if (providerName) {
      urlObj.searchParams.set('provider_name', providerName);
    }
    wsUrl = urlObj.toString();
  } catch (e) {
    console.error("Invalid BASE_WEBSOCKET_URL:", BASE_WEBSOCKET_URL);
    updateStatus(WebSocketProxyStatus.ERROR, "Invalid WebSocket URL configured.");
    return;
  }
  
  console.log(`WebSocket Proxy: Attempting to connect to ${wsUrl}`);

  try {
    socket = new WebSocket(wsUrl);
  } catch (error) {
    console.warn("WebSocket Proxy: Instantiation error:", error);
    updateStatus(WebSocketProxyStatus.ERROR, `Failed to instantiate WebSocket: ${error instanceof Error ? error.message : String(error)}`);
    scheduleReconnect(); 
    return;
  }

  socket.onopen = onSocketOpen;
  socket.onmessage = onSocketMessage;
  socket.onerror = onSocketError;
  socket.onclose = onSocketClose;
}

function disconnect() {
  explicitClose = true;
  currentJwtToken = null; 
  if (reconnectTimeoutId) {
    clearTimeout(reconnectTimeoutId);
    reconnectTimeoutId = null;
  }
  stopPing();
  if (socket) {
    if (socket.readyState === WS_OPEN || socket.readyState === WS_CONNECTING) {
      socket.close(1000, "Client initiated disconnect"); 
    } else {
      onSocketClose({ code: 1000, reason: "Client initiated disconnect on non-open socket", wasClean: true } as CloseEvent);
    }
  } else {
     updateStatus(WebSocketProxyStatus.IDLE, "Disconnected (no active socket).");
  }
  socket = null; 
}

function setOnStatusChange(callback: ((status: WebSocketProxyStatus, details?: string) => void) | null) {
  onStatusChangeCallback = callback;
  if (onStatusChangeCallback) {
    onStatusChangeCallback(currentStatus);
  }
}

export const webSocketProxyManager = {
  connect,
  disconnect,
  setOnStatusChange,
};

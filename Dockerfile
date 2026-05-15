# 使用一个轻量的 Python 官方镜像作为基础
FROM python:3.11-slim-bookworm

# 设置工作目录，后续的命令都在这个目录下执行
WORKDIR /app

# 安装运行 Playwright 所需的最小系统依赖集
# 安装必要的依赖，包括 VNC, 窗口管理器, 以及 NoVNC (用于网页访问)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 \
    libnspr4 libnss3 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxrandr2 libxrender1 libxtst6 ca-certificates \
    fonts-liberation libasound2 libpangocairo-1.0-0 libpango-1.0-0 libu2f-udev \
    xvfb x11vnc fluxbox novnc websockify \
    && rm -rf /var/lib/apt/lists/*

# 拷贝并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 下载 camoufox
RUN camoufox fetch

# 将项目中的所有文件拷贝到工作目录
COPY . .

# 修改启动脚本：启动 Xvfb -> VNC -> NoVNC网页服务 -> Python脚本
RUN echo '#!/bin/bash\n\
export DISPLAY=:99\n\
# 1. 启动虚拟显示器\n\
Xvfb :99 -screen 0 1280x800x24 &\n\
sleep 2\n\
# 2. 启动窗口管理器\n\
fluxbox &\n\
# 3. 启动 VNC 服务\n\
x11vnc -display :99 -nopw -listen localhost -xkb -forever &\n\
# 4. 启动 NoVNC (将 VNC 转为网页 WebSocket)\n\
websockify --web /usr/share/novnc/ 6080 localhost:5900 &\n\
\n\
echo "可以通过 http://localhost:6080/vnc.html 访问桌面了！"\n\
echo "启动主程序..."\n\
exec python main.py\n\
' > /app/start.sh

RUN chmod +x /app/start.sh

# 暴露 NoVNC 网页端口
EXPOSE 6080 
# 暴露 Hugging Face Spaces 期望的端口（仅在服务器模式下使用）
EXPOSE 7860

# 设置容器启动时要执行的命令
CMD ["/app/start.sh"]
#CMD ["python", "main.py"]

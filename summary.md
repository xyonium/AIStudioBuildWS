# Playwright 弹窗自动化：层叠弹窗点击锁死问题与解决方案

本文档记录 AI Studio（基于 Angular Material）启动阶段弹窗自动化的踩坑经验，
沉淀关于 Playwright + 异步串行弹窗场景的通用知识和易错点。

---

## 一、典型问题现象

1. **弹窗层叠导致点击锁死**
   应用初始化时先弹出一个弹窗（如 ToS 协议），随后又被新的弹窗覆盖。
   自动化脚本在新弹窗还未出现时就锁定了下层按钮的位置，之后持续点击
   已被覆盖的下层按钮，鼠标被锁死在失效区域，无法响应上层弹窗。

2. **左侧面板 / 周边 UI 干扰**
   页面侧栏（如建议卡片、导航箭头）中包含大量 `role="button"` 元素。
   全页无差别扫描时，这些元素可能被误识别为弹窗按钮，导致点击跑偏。

3. **异常重启引发鉴权失败**
   频繁崩溃重启后，Profile 中的认证状态（IndexedDB / LocalStorage）
   未能正确持久化，API 请求携带失效会话上下文，后端返回 401/403。

---

## 二、根因分析：旧轮询逻辑的四个致命缺口

| # | 缺口 | 详细 |
|---|------|------|
| 1 | **缺少弹窗稳定期（Debounce）** | `page.goto()` 完成后立即每秒轮询。弹窗是异步串行加载的：先挂下层 → 几百毫秒后再挂上层。脚本第 1 秒就锁定下层按钮，上层此时才刚开始渲染。 |
| 2 | **`bounding_box` 无法识别 z-index 遮挡** | 被上层弹窗覆盖的下层按钮仍然具有有效的宽高，`bounding_box` 校验通过，`force=True` 也会尝试点击，但实际上被上层遮罩拦截。 |
| 3 | **缺少"点击有效性"反馈闭环** | 逻辑是单向的"扫描 → 点击 → 继续轮询"，不验证点击后按钮/弹窗是否消失。只要下层按钮还在 DOM 中且可见，每一轮都会优先命中，形成死循环。 |
| 4 | **候选优先级固定，新弹窗抢不过** | 轮询顺序固定。下层的标准 `<button>` 在 `get_by_role` 中匹配度更高，新冒出的上层弹窗即使是同名按钮，若是非标准元素就抢不过已被锁定的下层。 |

---

## 三、设计原则

针对以上四个缺口，综合方案包含四根支柱：

1. **弹窗稳定检测（Debounce）**
   进入轮询前，先等待弹窗/遮罩层数量稳定，确保没有新的 overlay 再冒出来。
   实现方式：循环计数 `document.querySelectorAll('mat-mdc-dialog-container, .cdk-overlay-pane, [role="dialog"]')` 的 length，连续 N 秒不变才放行。

2. **只点击最上层弹窗**
   不在全页无差别扫描。每轮先确定最上层 dialog（DOM 序最后一个 + `getBoundingClientRect` 宽高 > 0），仅在其内部匹配按钮。

3. **`elementFromPoint` 命中测试**
   对候选按钮，计算其 bounding box 中心点，调用 `document.elementFromPoint(cx, cy)` 取该坐标实际占据的元素，
   校验 `topEl === target || target.contains(topEl)`。被遮罩 / 上层弹窗 / 侧栏卡片遮挡的按钮都会被过滤掉。

4. **点击有效性反馈 + 3 次失败黑名单**
   点击后验证按钮是否消失（弹窗关闭 → 按钮消失 = 点击有效）。
   同一按钮连续 3 次"点击后仍然可见"则加入临时 skip_set，下一轮优先尝试其他候选，
   有效点击后重置计数并将其从 skip_set 移除。

---

## 四、实施过程中发现的两个 Playwright 易错点

这两个 bug 都发生在"命中测试"的 JS 里，会**让整条策略静默失效**（按钮全部被过滤，等同于没有命中测试）。

### Bug 1：在浏览器原生 API 里用 Playwright 专属选择器

```python
# ❌ 错误写法
is_on_top = page.evaluate("""({x, y, selector}) => {
    const target = document.querySelector(selector);  // 这里会抛 SyntaxError
    ...
}""", {"x": cx, "y": cy, "selector": f'button:has-text("{btn_name}")'})
```

`:has-text(...)` 是 Playwright 自己的选择器引擎，**不是浏览器原生 CSS 伪类**。
`document.querySelector` 直接抛 `SyntaxError`，命中测试永远返回 `false`，
整条 `get_by_role` 路径形同虚设。

**正确做法**：把 Playwright 已经定位到的 `ElementHandle` 直接传进 JS，
浏览器拿到真实 DOM 节点后可直接与 `elementFromPoint` 比对，无需重新查询：

```python
# ✅ 正确写法
is_on_top = page.evaluate("""({element, x, y}) => {
    if (!element) return false;
    const top = document.elementFromPoint(x, y);
    return top === element || element.contains(top);
}""", {"element": handle.element_handle(), "x": cx, "y": cy})
```

### Bug 2：`page.evaluate` 只能接受一个 arg，多传的会被静默丢弃

```python
# ❌ 错误写法 — btn_name 被丢弃
is_on_top = page.evaluate("""({x, y}) => {
    ...
    return el.textContent.trim().includes(arguments[2]);  // undefined
}""", {"x": cx, "y": cy}, btn_name)
```

Playwright 的 `evaluate` 签名是 `evaluate(expression, arg)`，**只接受一个 arg**。
第 3 个参数 `btn_name` 被静默丢弃，JS 里 `arguments[2]` 是 `undefined`，
`text.includes(undefined)` 恒为 `false`，文本匹配策略形同虚设。

**正确做法**：把所有参数打包进单个 dict：

```python
# ✅ 正确写法
is_on_top = page.evaluate("""({x, y, btnName}) => {
    const top = document.elementFromPoint(x, y);
    ...
    return el.textContent.trim().includes(btnName);
}""", {"x": cx, "y": cy, "btnName": btn_name})
```

---

## 五、预期效果

- **弹窗层叠场景**：等待上层弹窗稳定后才开始扫描，命中测试确保只点到最上层。
- **点击失败场景**：连续 3 次无效后自动跳过，尝试其他按钮或等待新元素，避免死循环。
- **侧栏 / 周边 UI 干扰**：命中测试筛除被遮挡的元素，减少误点。
- **鉴权失败**：顺利完成启动后不再频繁重启，Profile 认证态能正常持久化。

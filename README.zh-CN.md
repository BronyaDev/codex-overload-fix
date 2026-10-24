# Codex 满载修复

一个本地重试中继，让 **「Selected model is at capacity. Please try a different model.」** 不再打断你干活。

某晚真实使用的前后对比：

| | 请求数 | 吞掉的重试 | 用户看到的失败 |
|---|---|---|---|
| 未修复 | — | — | 大约每两轮被打断一次 |
| 已修复 | 71 | 82 | **0** |

> 有一条 799 KB 的请求**连续失败 8 次**，第 9 次成功。用户全程什么都没看见。

---

## 问题是什么

Codex 会频繁弹出：

```
Selected model is at capacity. Please try a different model.
```

不是额度问题，不是网络问题，也不是模型真的满了。这个错误是**HTTP 200 的 SSE 流里的一个 `error` 事件**：

```json
{"type":"error","sequence_number":3,
 "error":{"type":"service_unavailable_error",
          "code":"server_is_overloaded",
          "message":"Our servers are currently overloaded. Please try again later."}}
```

因为状态码是 200，**任何按状态码重试的东西都看不见它**——代理看不见、HTTP 库看不见、客户端自己的重试层也看不见。整条流读完才判定失败，默认预算用完，然后告诉用户「换个模型试试」。

这句话是误导。下面说明为什么。

---

## 实际发生了什么

拒绝率跟**请求体大小**相关，跟模型选择无关。

同一晚、同一账号的 71 个 `/responses` 请求，效果**不是渐变，是断崖**：

| 请求体 | 数量 | 平均尝试次数 | 最惨 |
|---|---|---|---|
| < 100 KB | 20 | 1.95 | 5 |
| 100–200 KB | 24 | 1.96 | 9 |
| 200–500 KB | 23 | 1.74 | 4 |
| **> 500 KB** | **4** | **6.75** | **9** |

500 KB 以下，各档几乎没有区别，都落在 1.7–2.0 之间；跨过 500 KB，均值直接跳到 6.8，最惨 9 次。430–768 KB 之间没有样本，所以确切的阈值还定不下来。

另外两点：

- **并发会叠加。** 最坏的情况是同一秒内发出三个约 770 KB 的请求。这更符合「按分钟窗口准入」，而不是单请求随机。
- **大请求往往是任务的开场白。** 带参考图的任务，第一轮就可能是 700+ KB——而这一轮恰恰最不能失败。

桌面版新会话的请求体（约 89 KB）由这些组成：

| 组成 | 体积 |
|---|---|
| `additional_tools`（工具定义） | 27.6 KB |
| 基础系统提示 | 17.7 KB |
| 技能清单（装了 72 个技能时） | 20.4 KB |
| 记忆块 | 12.8 KB |

而长期会话，App 自己的日志里 `pre_compression_bytes ≈ 724,000`——会话历史压倒性地最大。**开新会话是单项收益最大的一步。**

---

## 这个工具做什么

`relay/relay.py` 是一个单文件、纯标准库的 HTTP 中继。它站在 Codex 和真实端点之间，做三件事：

1. **认出 overload。** 逐行解析 SSE，匹配 `server_is_overloaded`、`server_overloaded`、`service_unavailable_error`、`flex_unavailable`、`queue_full`，以及 HTTP 429/5xx。
2. **只在 commit 之前重试。** 一旦吐出过任何 `response.output_text.delta` 或 `response.reasoning_summary_text.delta`，就直接放行原流。永远不会生成两遍。
3. **你停它就停。** 它会窥探客户端 socket，Codex 取消回合或退出时立即放弃重试，不空烧额度。

其余全部字节透明：请求体（含 zstd 压缩的）、鉴权头、SSE 内容原样转发，不做任何修改。

---

## 快速开始（Windows）

```powershell
git clone https://github.com/NewBeeM4n/codex-overload-fix.git
cd codex-overload-fix
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

安装脚本会：

1. 找到 Python（3.9+，纯标准库，无依赖）。
2. 在 `HKCU\...\Run` 里注册隐藏启动器和看门狗——不需要管理员权限，不弹控制台窗口。
3. 打印你需要粘贴的 `config.toml` 片段。

然后编辑 `%USERPROFILE%\.codex\config.toml`：

```toml
model_provider = "openai-retry"

[model_providers.openai-retry]
name = "OpenAI (retry)"
base_url = "http://127.0.0.1:8787"
wire_api = "responses"
requires_openai_auth = true      # 复用现有 ChatGPT OAuth，不用重新登录
stream_max_retries = 20
request_max_retries = 10
```

重启 Codex。跑一个回合时看 `127.0.0.1:8787` 有没有 ESTABLISHED 连接，就能确认中继在工作。

> `openai` 是保留 provider id，不能被覆盖，所以这里定义了一个新的。

### 如果你用代理

把代理传给中继：

```powershell
python relay\relay.py --port 8787 --proxy http://127.0.0.1:7897 --log relay.log
```

中继必须能通过你 Codex 平时用的出口访问 `chatgpt.com`。如果机器级代理已经覆盖了，就不用加 `--proxy`。

### 手动启动

```powershell
python relay\relay.py --port 8787 --log relay.log --max-attempts 30 --backoff-base 1 --backoff-max 8
```

---

## 安全吗

- **不碰你的凭证。** 转发时用的就是 Codex 自己发出的头，包括你的 OAuth token。中继不存储、不记录、不发送到任何其他地方。
- **不修改请求体。** 逐字节转发。
- **不绕过任何限制。** 它只是重试一个服务端拒绝服务的请求。用的是官方支持的 custom provider 机制——很多人用它把 Codex 指向 Ollama 或 LM Studio。
- **它是本地的。** `127.0.0.1` 上的一个 Python 进程。从 `config.toml` 删掉一行就回到直连。
- **只在什么都没生成时重试**，所以不可能出现重复或拼接的输出。

它**治不了**：额度真正用尽、登录过期、网络不通。那些是不同的错误，会原样透传。

---

## 诊断工具

| 工具 | 作用 |
|---|---|
| `tools/capture_server.py` | 冒充端点，保存 Codex 实际发出的请求体，回一条合法的 SSE 流。用来测你自己的请求体积。 |
| `tools/analyze_log.py` | 读 `relay.log`，统计请求数、重试数，以及体积与尝试次数的分布。 |

```powershell
# 测你的客户端实际发多大
python tools\capture_server.py outdir 18090
# 然后把一个临时 provider 指到 http://127.0.0.1:18090

# 汇总中继日志
python tools\analyze_log.py relay.log
```

---

## 实测无效的做法

同一台机器、同一账号、多轮测试：

| 做法 | 结果 |
|---|---|
| 换出口节点 | 五个网络 30/30 全部成功，拒绝率不变 |
| 在坏窗口里换模型 | 所有模型一起变差 |
| 改系统时区 | 连改 5 次，毫无变化 |
| 改 `service_tier` | 交替 8 组，无差别 |
| 重启 App | 只影响运气，不影响概率 |

---

## 上游应该怎么改

丢负载这个机制是对的——这个量级下，服务端排队在客户端超时重试时会变成雪崩。问题在于**丢的方式**：

- 准入失败应该是 **503 + `Retry-After`**，而不是 200 流里的一个 `error` 事件。这样所有反向代理、HTTP 库、重试层都能自动处理。
- 如果必须留在流内，客户端应该把 `server_is_overloaded` **默认当作可重试**，带退避，而不是透传给用户。
- 这一类错误的默认重试预算应该远高于 5 次。
- 对于看起来是基于体积的准入决策，「试试别的模型」是误导性的建议。

---

## 局限

- 单机、单账号、样本小。体积相关性是信号，具体百分比当噪声看。
- 请求体积数字来自某一个 Codex 版本。信绝对值之前请先用 `tools/capture_server.py` 自己量一次。
- 中继在重试期间会一直挂着请求。重试预算很大 + 链路很慢时，一个回合可能「思考」很久才失败。
- 启动脚本是 Windows 的；中继本身跨平台，有 Python 就能跑。

---

## 许可证

MIT — 见 [LICENSE](LICENSE)。

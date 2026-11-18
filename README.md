# yuketang-sentinel · 雨课堂习题哨兵

> A read-only Rain Classroom (雨课堂) companion: it watches your live class, alerts you the moment the teacher pushes a quiz, and asks an LLM for a reference answer. It never answers, never submits, and never fakes presence — you stay in the loop.

监听正在进行的雨课堂课程：老师推送随堂习题时，立即通过 macOS 通知（可选 Bark / Server酱 / 企业微信推送到手机）提醒你，并调用大模型（任意 OpenAI 兼容接口）给出参考答案，答案自动复制到剪贴板方便你在手机上粘贴提交。

**设计边界：只提醒、只解读。不自动作答、不自动提交、不伪造定位/点名。** 是否作答、答什么，始终由使用者本人决定与操作。

支持站点：长江雨课堂（changjiang）/ 雨课堂（www）/ 荷塘雨课堂（pro）/ 黄河雨课堂（huanghe）。

## 特性

- **自动接管课堂**：每 45 秒扫描"正在上课"，发现后自动进班、拉取课件、缓存全部随堂题
- **秒级发题感知**：WebSocket `unlockproblem` 推送 + `fetchtimeline` 轮询双通道，去重不漏报
- **占位题面读图**：题干印在 PPT 页面图里（结构化字段为占位文本）的题目，自动下载页面图转视觉模型读图作答
- **剪贴板接力**：参考答案生成后自动复制，配合 iPhone 通用剪贴板可在手机上直接粘贴
- **动态二维码课支持**：本人微信扫码进班后，哨兵下一轮扫描自动接管；或用 `signin --url` 把扫码得到的链接交给程序接力进班
- **课件热更新**：老师中途换课件自动重拉新题
- **多课并发**：课程名白名单过滤；下课自动退出，登录态失效主动提醒，配置文件热更新
- **手机推送**（可选）：Bark / Server酱 / 企业微信机器人，任配其一

## 安装

macOS / Linux，Python 3.9+：

```bash
git clone https://github.com/JynnaiSanlya/yuketang-sentinel.git
cd yuketang-sentinel
pip3 install --user -r requirements.txt
```

`qrcode` 仅终端扫码登录时需要，可省略。

## 配置

```bash
cp config.example.json config.json
python3 ykt_sentinel.py login     # 终端打印二维码，微信扫码后 sessionid 自动写回
python3 ykt_sentinel.py whoami    # 校验登录态
```

不想扫码：浏览器登录雨课堂网页版 → F12 → Application → Cookies，把 `sessionid`（建议连同 `csrftoken`）填进 config.json。sessionid 有效期数天到两周，失效时哨兵会主动提醒，运行中更新 config.json 自动生效，无需重启。

关键字段：

| 字段 | 说明 |
| --- | --- |
| `server` / `base_url` | 站点选择，或直接写完整域名 |
| `course_filter` | 课程名白名单（子串匹配），空 = 监听全部正在上的课 |
| `scan_interval_seconds` | 无课时扫描间隔 |
| `poll_interval_seconds` | 课堂内轮询间隔（推送丢失兜底） |
| `notify_mac` / `notify_sound` | macOS 通知开关与提示音 |
| `copy_answer_clipboard` | 答案自动复制到剪贴板 |
| `push.*` | 手机推送三选一：`bark_url` / `serverchan_sendkey` / `wecom_webhook` |
| `llm.*` | 任意 OpenAI 兼容接口；`vision_model` 用于读图题（推荐配一个多模态模型） |

## 使用

```bash
python3 ykt_sentinel.py courses   # 我的课程
python3 ykt_sentinel.py lessons   # 正在进行的课堂
python3 ykt_sentinel.py run       # 挂机监控（默认子命令）
```

上课前建议用 `caffeinate -i python3 ykt_sentinel.py run` 启动，防止 Mac 休眠断开监听。

### 动态二维码课堂

部分课堂开启动态二维码强制签到（进班接口返回 `DYNAMIC_QR_CHECK_IN_REFUSED`）。这类课堂的出勤必须由**本人**微信扫描大屏幕上的动态二维码——这是平台的有意设计，本工具不支持也不会尝试绕过。

你本人扫码进班后，哨兵会在下一轮扫描（≤45 秒）自动重试接管监听并开始工作；也可以把微信中"复制链接"得到的扫码链接交给程序：

```bash
python3 ykt_sentinel.py signin --url '<扫码得到的链接>'
```

## 工作原理

```
扫描循环（默认45s）                     课堂 WebSocket（wss://<host>/wsapp/）
GET /api/v3/classroom/           hello 认证 → 拉取课件全量题目缓存
on-lesson-upcoming-exam          ← unlockproblem 推送（老师发题）
  → 发现上课 → checkin 进班  →    + fetchtimeline 轮询兜底
  → 换取 Bearer/lessonToken       → 通知①：有新题（题型/限时/题干）
                                  → LLM/读图解答 → 通知②：参考答案
```

雨课堂的随堂题嵌在课件中，进班时即可全量获取；老师"发题"只是服务端解锁题目 ID。题面为占位文本的当堂题，自动下载所在 PPT 页渲染图交视觉模型解读。协议事实来自公开研究（见"参考"），平台协议变更可能导致失效。

## 设计边界与声明

- 本工具**只读**：除"进课堂"（等同你自己扫码）外不向平台提交任何数据；不代答、不提交、不做位置签到、不自动确认点名。
- AI 参考答案可能出错，请核对后再作答；因使用本工具产生的任何学业/纪律后果由使用者自行承担，请遵守所在学校的规定。
- 自动化签到类功能不在本项目范围内；动态二维码签到依赖本人现场扫码。

## 已知限制

- 完全独立于课件、随机生成的当堂题拿不到题面（只能提醒你看手机）；若发题时题目会挂到课件上，重拉后仍可读图解答
- 图片题需要 `vision_model`，未配置时只提醒不解读
- 位置签到、随机点名的确认动作需要你本人在手机上完成（哨兵只提醒）
- 服务器 WebSocket/协议调整会导致功能失效

## 测试

```bash
python3 -m unittest discover -s tests -v   # 21 项离线用例，不联网
```

## 参考

协议与实现思路参考了以下公开项目，感谢原作者：

- [2008lmf/yuketang-helper](https://github.com/2008lmf/yuketang-helper)（MIT）— 雨课堂协议文档与多站点实现
- [Chiangjiang-RainClassroom-Auto](https://github.com/skh2945932142/Fuck-Yangtze-RainClassroom)（Apache-2.0）— 长江站点 WebSocket 事件、fetchtimeline 轮询等实测行为
- [Infinite-Galaxy-Lab/rainclass-reminder](https://github.com/Infinite-Galaxy-Lab/rainclass-reminder)（MIT）— 只读提醒工具的登录态与推送通道设计

本项目为独立实现，未复制上述项目的源代码。

## License

[MIT](LICENSE)

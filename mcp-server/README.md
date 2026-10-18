# ASR MCP Server（语音识别统一接口）

「东阿县人民医院 AI 病历智能体」的语音识别入口：上层（Demo 录音步骤、P1 多智能体生成器）只面向 MCP 工具和统一 Transcript 格式，底层引擎可插拔——这正是 PRD 4.2"ASR 为可替换模块"的工程落地。

## 架构

```
上层调用方（Demo 录音步骤 / P1 generate_llm / 未来医生端服务）
        │  MCP stdio（JSON-RPC 2.0）
        ▼
┌──────────────────────────────────────┐
│ asr_mcp_server.py                     │
│  asr_engines / asr_prepare_audio /    │
│  asr_transcribe → 统一 Transcript     │
├──────────────────────────────────────┤
│ 引擎适配层（BaseEngine 契约）          │
│  mock ✓       结构演示与链路联调      │
│  xfyun        讯飞 RTASR（密钥后补）  │
│  aliyun       阿里云文件转写（密钥后补）│
│  sensetime    商汤（接口资料待确认）   │
│  local-whisper 本地私有化对照（无外呼）│
│  funasr ✓     自建主力：SenseVoice/SeACo│
│               （VAD分段+医疗热词偏置） │
└──────────────────────────────────────┘
```

- 统一 Transcript：`{engine, duration_sec, text, segments[{start,end,speaker,text}], mock, warnings}`，对齐 PRD F2（角色分离、溯源到录音时间点）。
- 核心零依赖（Python 3.9+）；各引擎依赖（websocket-client / requests / faster-whisper）仅在启用该引擎时才需要安装。
- 音频预处理内置：m4a/mp3 → 16kHz 单声道 WAV（ffmpeg 或 macOS afconvert），已用门诊真实音频验证。

## 接入 MCP 客户端（示例）

```json
{
  "mcpServers": {
    "donge-asr": {
      "command": "/usr/bin/python3",
      "args": ["/绝对路径/AI病历智能体系统/mcp-server/asr_mcp_server.py"],
      "env": {
        "ASR_DEFAULT_ENGINE": "mock",
        "ASR_XFYUN_APP_ID": "",
        "ASR_XFYUN_API_KEY": ""
      }
    }
  }
}
```

## 自测

```bash
python3 mcp-server/test_client.py
# 6 项断言：握手 / 工具清单 / 引擎状态 / 真实m4a转码 / mock转写(角色分离) / 未配置引擎的结构化报错
```

## 拿到密钥后的接入步骤

| 引擎 | 步骤 | 注意 |
|---|---|---|
| 讯飞 RTASR | 控制台开通"实时语音转写"→ 填 `ASR_XFYUN_APP_ID/API_KEY` → `pip install websocket-client` → 跑 test_client，核对 `_signa` 签名与结果消息约定（代码内已标 TODO） | 长录音流式、支持医疗热词定制（对齐 PRD 4.2）；**单声道不带角色**，说话人分离由 P1 后处理实现 |
| 阿里云文件转写 | 开通智能语音交互 → 填三个环境变量 → `pip install requests` → 按官方 SDK 实现 Token 获取与任务提交（骨架已留） | 要求音频 URL 可访问（OSS），需配套院内文件上传通道；自带说话人分离 |
| 商汤 | 接口资料确认后在 `SenseTimeEngine.transcribe` 按 `BaseEngine` 契约实现 | 目前为占位适配器 |
| 本地 faster-whisper | `pip install faster-whisper` → `ASR_LOCAL_MODEL=small`（或 large-v3-turbo 等档位/模型路径） | 无外呼；`ASR_LOCAL_BEAM` 缺省 1=贪心（患者F 实测波束劣化）；`ASR_LOCAL_DEVICE/COMPUTE` 控设备 |
| 本地 FunASR（自建主力） | `pip install funasr torch torchaudio` → 即用（缺省 SenseVoiceSmall，模型经 ModelScope 缓存后可离线） | 无外呼；VAD 分段+超长段能量切分（>15~20s 长段实测退化）；SeACo 模型支持**医疗热词解码期偏置**（`ASR_FUNASR_MODEL` 切模型、`ASR_FUNASR_CHUNK_S/PUNC/VAD` 调分段） |

## 自建 ASR 引擎（不依赖外部 API）

`funasr` 与 `local-whisper` 为**本地自建引擎**：零外呼、数据不出机，是私有化交付（方案 B）与"不依赖云 API"路径的实现载体。选型实测（患者F 真实方言录音 CER + 关键事实保留率）见 `docs/自建ASR选型与实施路径.md`；切换默认引擎：`ASR_DEFAULT_ENGINE=funasr`（或 local-whisper），上层与后处理管线零改动。

## 与评测体系的关系

引擎接入后：先在 `evaluation/asr_testset/`（门诊 5 段真实音频 + 人工参考文本）上跑 CER 选型对比；胜出引擎再接入 `evaluation/evaluate_v0.py` 的录音链路重跑端到端基线。**任何引擎不得以 mock 输出参与评测或归档**（Transcript.mock 字段即为此校验预留）。

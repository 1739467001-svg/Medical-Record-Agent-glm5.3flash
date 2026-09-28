# -*- coding: utf-8 -*-
"""MCP 服务器端到端自测：握手 → tools/list → asr_engines → 真实音频预处理 → mock 转写。"""
import json, os, subprocess, sys, tempfile

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asr_mcp_server.py")
PROJECT = os.path.dirname(os.path.dirname(SERVER))

class Client:
    def __init__(self):
        self.p = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
    def call(self, method, params=None, id=None):
        req = {"jsonrpc": "2.0", "method": method}
        if params is not None: req["params"] = params
        if id is not None: req["id"] = id
        self.p.stdin.write(json.dumps(req) + "\n"); self.p.stdin.flush()
        if id is None: return None
        while True:
            line = self.p.stdout.readline()
            if not line: raise RuntimeError("server closed")
            resp = json.loads(line)
            if resp.get("id") == id: return resp

def main():
    audio = os.path.join(PROJECT, "病历资料", "门诊", "5.m4a")
    c = Client()
    r = c.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}, 1)
    assert r["result"]["serverInfo"]["name"] == "donge-asr-mcp", r
    print("[1] initialize OK:", r["result"]["serverInfo"])
    c.call("notifications/initialized")
    r = c.call("tools/list", {}, 2)
    tools = [t["name"] for t in r["result"]["tools"]]
    assert tools == ["asr_engines", "asr_prepare_audio", "asr_transcribe"], tools
    print("[2] tools/list OK:", tools)
    r = c.call("tools/call", {"name": "asr_engines", "arguments": {}}, 3)
    engines = json.loads(r["result"]["content"][0]["text"])
    print("[3] asr_engines OK:", {k: v.get("ready") for k, v in engines.items() if not k.startswith("_")})
    r = c.call("tools/call", {"name": "asr_prepare_audio",
                              "arguments": {"audio_path": audio, "out_dir": tempfile.mkdtemp()}}, 4)
    wav = json.loads(r["result"]["content"][0]["text"])["wav_16k"]
    assert os.path.exists(wav) and os.path.getsize(wav) > 1000, wav
    print(f"[4] asr_prepare_audio OK: {os.path.basename(wav)} ({os.path.getsize(wav)} bytes)")
    r = c.call("tools/call", {"name": "asr_transcribe",
                              "arguments": {"audio_path": wav, "engine": "mock", "speakers": True}}, 5)
    t = json.loads(r["result"]["content"][0]["text"])
    assert t["engine"] == "mock" and t["segments"] and t["segments"][0]["speaker"] == "医生", t
    print(f"[5] asr_transcribe OK: {len(t['segments'])} 段 · 首段[{t['segments'][0]['speaker']}] {t['segments'][0]['text'][:24]}…")
    # 未配置引擎应返回结构化错误而非崩溃
    r = c.call("tools/call", {"name": "asr_transcribe",
                              "arguments": {"audio_path": wav, "engine": "xfyun"}}, 6)
    assert r["result"].get("isError") and "未配置" in r["result"]["content"][0]["text"]
    print("[6] 未配置引擎错误处理 OK:", r["result"]["content"][0]["text"][:38], "…")
    c.p.terminate()
    print("\nALL MCP TESTS PASSED")

if __name__ == "__main__":
    main()

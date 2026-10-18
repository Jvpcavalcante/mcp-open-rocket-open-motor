"""Servidor como subprocesso, do jeito que o host o executa: o stdout só pode ter JSON-RPC e o processo
precisa terminar sozinho quando o stdin fecha (P2 do diário: a JVM segurava o processo)."""
import json
import os
import queue
import subprocess
import sys
import threading

import pytest

pytestmark = pytest.mark.stdio


def _mensagem(**kw):
    return (json.dumps({"jsonrpc": "2.0", **kw}) + "\n").encode()


def test_stdout_so_jsonrpc_e_sai_apos_eof(workspace):
    p = subprocess.Popen([sys.executable, "-m", "rocket_mcp.server"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         env={**os.environ, "ROCKET_MCP_WORKSPACE": str(workspace)})
    linhas: queue.Queue[bytes] = queue.Queue()
    threading.Thread(target=lambda: [linhas.put(ln) for ln in p.stdout], daemon=True).start()
    try:
        p.stdin.write(_mensagem(id=1, method="initialize", params={
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}}))
        p.stdin.write(_mensagem(method="notifications/initialized"))
        # abrir_foguete força o boot completo da JVM e a carga do .ork (o que mais gera log)
        p.stdin.write(_mensagem(id=2, method="tools/call",
                                params={"name": "abrir_foguete", "arguments": {"arquivo_ork": "foguete.ork"}}))
        p.stdin.flush()

        respostas = {}
        while 2 not in respostas:
            linha = linhas.get(timeout=120)
            msg = json.loads(linha)  # falha se algo além de JSON-RPC sair no stdout
            if "id" in msg:
                respostas[msg["id"]] = msg
        assert "result" in respostas[1]
        assert respostas[2]["result"]["isError"] is False

        p.stdin.close()
        assert p.wait(timeout=30) == 0
        while not linhas.empty():
            json.loads(linhas.get())
    finally:
        if p.poll() is None:
            p.kill()

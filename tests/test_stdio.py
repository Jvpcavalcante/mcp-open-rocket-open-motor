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


def _servidor(workspace):
    p = subprocess.Popen([sys.executable, "-m", "rocket_mcp.server"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         env={**os.environ, "ROCKET_MCP_WORKSPACE": str(workspace)})
    linhas: queue.Queue[bytes] = queue.Queue()
    threading.Thread(target=lambda: [linhas.put(ln) for ln in p.stdout], daemon=True).start()
    p.stdin.write(_mensagem(id=1, method="initialize", params={
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}}))
    p.stdin.write(_mensagem(method="notifications/initialized"))
    return p, linhas


def _esperar(linhas, ids, timeout=120):
    respostas = {}
    while not ids <= respostas.keys():
        msg = json.loads(linhas.get(timeout=timeout))  # falha se algo além de JSON-RPC sair no stdout
        if "id" in msg:
            respostas[msg["id"]] = msg
    return respostas


def test_stdout_so_jsonrpc_e_sai_apos_eof(workspace):
    p, linhas = _servidor(workspace)
    try:
        # abrir_foguete força o boot completo da JVM e a carga do .ork (o que mais gera log)
        p.stdin.write(_mensagem(id=2, method="tools/call",
                                params={"name": "abrir_foguete", "arguments": {"arquivo_ork": "foguete.ork"}}))
        p.stdin.flush()
        respostas = _esperar(linhas, {1, 2})
        assert "result" in respostas[1]
        assert respostas[2]["result"]["isError"] is False

        p.stdin.close()
        assert p.wait(timeout=30) == 0
        while not linhas.empty():
            json.loads(linhas.get())
    finally:
        if p.poll() is None:
            p.kill()


def test_relatorio_pedido_durante_o_boot_da_jvm_nao_trava(workspace):
    """Regressão: importar o matplotlib (DLLs) enquanto a JVM inicializava travou o processo no Windows.
    O relatório é pedido logo após o handshake, com a JVM ainda subindo."""
    p, linhas = _servidor(workspace)
    try:
        p.stdin.write(_mensagem(id=2, method="tools/call", params={"name": "gerar_relatorio", "arguments": {
            "arquivo_ork": "foguete.ork", "arquivo_html": "stdio/relatorio_boot.html", "sobrescrever": True}}))
        p.stdin.flush()
        respostas = _esperar(linhas, {1, 2}, timeout=150)
        assert respostas[2]["result"]["isError"] is False, respostas[2]
        assert (workspace / "stdio" / "relatorio_boot.html").exists()
    finally:
        p.kill()

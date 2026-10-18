# rocket-mcp
Servidor MCP (Python, SDK mcp 2.x) unificando openMotor (motorlib) e OpenRocket 24.12 (JPype).
Especificação completa, armadilhas e código inicial: docs/ANALISE.md — leia antes de mudar a arquitetura.

## Invariantes (não quebrar)
- stdout é exclusivo do JSON-RPC: logs só em stderr (inclusive logback do OpenRocket).
- Todo acesso a OpenRocket/motorlib passa pelo LOCK global de or_bridge.
- Caminhos vindos do LLM passam por _caminho() (sandbox do workspace).
- Erros para o LLM: ToolError. Unidades SI internas; sufixo de unidade nos parâmetros das tools.
- Rodar `python teste_cliente.py` (e pytest, quando existir) antes de cada commit.

## Estado atual
Ver docs/DIARIO.md.
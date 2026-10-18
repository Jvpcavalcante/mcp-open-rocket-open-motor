"""Teste ponta a ponta: sobe o servidor via stdio e chama as tools como um cliente MCP faria."""
import asyncio, os, sys, time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Workspace com um foguete.ork e um motor.ric (ex.: copie de vendor/openMotor/test/data/regression/tiny/motor.ric)
WORKSPACE = os.environ.get("ROCKET_MCP_WORKSPACE", os.path.abspath("workspace_teste"))

async def main():
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "rocket_mcp.server"],
        env={**os.environ, "ROCKET_MCP_WORKSPACE": WORKSPACE})  # herda JAVA_HOME, SYSTEMROOT (Windows)...
    t = time.time()
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize(); print(f"initialize: {time.time()-t:.1f}s")
        print("tools:", [x.name for x in (await s.list_tools()).tools])
        prog = []
        async def cb(p, total, msg): prog.append(p)
        for nome, args, kw in [
            ("simular_motor", {"arquivo_ric": "motor.ric"}, {"progress_callback": cb}),
            ("abrir_foguete", {"arquivo_ork": "foguete.ork"}, {}),
            ("projetar_e_voar", {"arquivo_ric": "motor.ric", "arquivo_ork": "foguete.ork", "diametro_mm": 29,
              "comprimento_mm": 150, "massa_hardware_kg": 0.12, "atraso_ejecao_s": 5}, {}),
            ("simular_voo", {"arquivo_ork": "../etc/passwd"}, {}),
        ]:
            t = time.time(); res = await s.call_tool(nome, args, **kw)
            txt = res.content[0].text if res.content else ""
            print(f"\n== {nome} ({time.time()-t:.1f}s) erro={res.is_error}\n{txt[:1500]}")
        print("\nnotificações de progresso recebidas:", len(prog), "última:", prog[-1] if prog else None)

asyncio.run(main())

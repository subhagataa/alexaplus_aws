"""Quick terminal chat with the CareLoop agent (needs server.py running): python cli.py"""
import asyncio

import agent
import llm


def root_cause(e: BaseException) -> BaseException:
    while isinstance(e, BaseExceptionGroup):
        e = e.exceptions[0]
    return e


async def main():
    print(f"Mode: {agent.MODE} | provider: {llm.PROVIDER} | model: {llm.MODEL} | MCP: {agent.MCP_URL}")
    print("Type a request (Ctrl+C to quit).")
    while True:
        text = input("you> ")
        try:
            out = await agent.run_agent("chat", text, "cli")
        except Exception as e:
            err = root_cause(e)
            print(f"  [error] {type(err).__name__}: {err}")
            continue
        if out["note"]:
            print(f"  [note] {out['note']}")
        for s in out["steps"]:
            print(f"  [tool] {s['tool']}({s['args']}) -> {s['result']}")
        print("alexa>", out["text"])


if __name__ == "__main__":
    asyncio.run(main())

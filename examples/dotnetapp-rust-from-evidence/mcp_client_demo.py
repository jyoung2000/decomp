"""External-client demo: talks to the real Rebuild Studio MCP server over stdio (as Claude Code/Codex/Gemini would)."""
import asyncio, json, sys
from mcp import Client
from mcp.client.stdio import StdioServerParameters

DATA = "/tmp/claude-0/-home-user-decomp/c456138f-a927-5bb9-bf2d-fa1845ce6ed1/scratchpad/dotnet-demo"
PARAMS = StdioServerParameters(command="/opt/rebuild-tools/venv/bin/python", args=["-m", "rebuild_controller.cli.main", "mcp", "--data-dir", DATA, "--runner", "auto", "--toolset", "rebuild"],
                               env={"REBUILD_STUDIO_DATA": DATA, "PATH": "/usr/bin:/bin:/usr/local/bin:/root/.cargo/bin", "HOME": "/root"}, cwd="/home/user/decomp/controller")

def first_case(cases):
    c = cases
    for k in ("result", "data"):
        if isinstance(c, dict) and k in c:
            c = c[k]
    if isinstance(c, dict) and "cases" in c:
        c = c["cases"]
    return c[0]["case_id"]

async def call(client, _tool, **args):
    r = await client.call_tool(_tool, args)
    txt = "".join(getattr(c, "text", "") for c in r.content)
    try:
        return json.loads(txt)
    except ValueError:
        return {"raw": txt, "is_error": r.is_error}

async def main():
    cmd = sys.argv[1]
    async with Client(PARAMS) as client:
        if cmd == "tools":
            tools = await client.list_tools()
            print([t.name for t in tools.tools])
        elif cmd == "raw":
            r = await call(client, sys.argv[2], **json.loads(sys.argv[3] if len(sys.argv) > 3 else "{}")); print(json.dumps(r, indent=1))
        elif cmd == "explore":
            cases = await call(client, "list_cases")
            cid = first_case(cases)
            mods = await call(client, "list_modules", case_id=cid)
            print(json.dumps(mods)[:600])
            mid = (mods.get("modules") or mods)[0]["module_id"]
            feats = await call(client, "list_features", case_id=cid)
            print("FEATURES", json.dumps(feats)[:1500])
            for fn in sys.argv[2:]:
                b = await call(client, "get_function_briefing", case_id=cid, module_id=mid, function=fn)
                print("=== BRIEFING", fn); print(json.dumps(b, indent=1)[:12000])
        elif cmd == "search":
            cases = await call(client, "list_cases"); cid = first_case(cases)
            r = await call(client, "search_evidence", case_id=cid, query=sys.argv[2], limit=20)
            print(json.dumps(r, indent=1)[:6000])
        elif cmd == "evidence":
            r = await call(client, "get_evidence", evidence_id=sys.argv[2], max_bytes=int(sys.argv[3]) if len(sys.argv) > 3 else 60000)
            print(json.dumps(r, indent=1)[:60000])
        elif cmd == "drive":
            cases = await call(client, "list_cases"); cid = first_case(cases); cand = sys.argv[2]
            c = await call(client, "compare_candidate", case_id=cid, candidate_id=cand); print("COMPARE", json.dumps(c)[:200])
            jid = c["data"]["job"]["job_id"]
            while True:
                await asyncio.sleep(3)
                js = await call(client, "job_status", job_id=jid)
                job = js["data"]["job"]
                if job["state"] in ("completed", "failed", "blocked", "cancelled"):
                    print("JOB", job["state"], json.dumps(job.get("result") or job.get("error") or job.get("blocker"))[:3000]); break
        elif cmd == "propose":
            cases = await call(client, "list_cases"); cid = first_case(cases)
            files = json.load(open(sys.argv[2]))
            r = await call(client, "propose_candidate", case_id=cid, files=files, note=sys.argv[3] if len(sys.argv) > 3 else "external client proposal")
            print("PROPOSED", json.dumps(r)[:800])
            dd = r.get("data", r)
            cand = dd.get("candidate_id") or (dd.get("candidate") or {}).get("candidate_id")
            print("CANDIDATE", cand)
            b = await call(client, "build_candidate", case_id=cid, candidate_id=cand); print("BUILD", json.dumps(b)[:300])
if __name__ == '__main__':
    asyncio.run(main())

from urllib.parse import urlsplit
import os
import stat
from .core import public_text
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.routes import build_metadata
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse
from .auth import OAuth, Vault
from .core import Registry, Queue, SafeFiles
from .task_packet import TaskTitle, TaskInstructions, TaskAcceptance, TaskKey

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def build(config,instance,runtime,vault=None):
    registry=Registry(config)
    base=instance["public_base_url"].rstrip("/")
    parsed=urlsplit(base)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path or (parsed.scheme!="https" and not(parsed.scheme=="http" and parsed.hostname=="127.0.0.1")):
        raise ValueError("Use HTTPS origin, or loopback HTTP for local testing")
    oauth=OAuth(runtime,vault or Vault(instance["instance_id"]),base,registry.projects)
    mcp=FastMCP("Multi-project Architect Bridge",instructions="Use only the project selected at OAuth approval. Source access is read-only. Submit one task; wait for result; review with ACCEPT/FIX. Never claim tests passed without executor evidence.",auth_server_provider=oauth,
        auth=AuthSettings(issuer_url=base,resource_server_url=base+"/mcp",required_scopes=["bridge"],client_registration_options=ClientRegistrationOptions(enabled=True,valid_scopes=["bridge"]+["project:"+p for p in registry.projects],default_scopes=["bridge"]+["project:"+p for p in registry.projects]),revocation_options=RevocationOptions(enabled=True)),
        host="127.0.0.1",port=instance["port"],stateless_http=True,json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,allowed_hosts=["127.0.0.1:*","localhost:*",parsed.netloc],allowed_origins=[base]))
    queues={p:Queue(v) for p,v in registry.projects.items()}
    def project(pid):
        token=get_access_token()
        if not token or "project:"+pid not in token.scopes: raise ValueError("Project access denied")
        return registry.project(pid)

    def architecture(p):
        path=p["root"]/".ai-bridge"/"architecture.md"
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise ValueError("Unsafe architecture file")
            return public_text(os.read(fd,24000).decode())
        finally: os.close(fd)

    @mcp.tool(annotations=READ)
    def project_info(bridge_project_id:str)->dict:
        """Read project binding and durable task state."""
        p=project(bridge_project_id)
        return {"bridge_project_id":bridge_project_id,"name":p.get("name"),"chatgpt_thread":p["chatgpt_thread"],"mode":p["mode"],"chatgpt_access":p["chatgpt_access"],"architecture":architecture(p),"state":queues[bridge_project_id].status()}

    @mcp.tool(annotations=READ)
    def list_files(bridge_project_id:str,path:str=".")->list[dict]:
        """List approved files/directories. Secrets, symlinks and runtime data excluded."""
        return SafeFiles(project(bridge_project_id)).list(path)

    @mcp.tool(annotations=READ)
    def read_file(bridge_project_id:str,path:str)->str:
        """Read one approved UTF-8 source/document file, at most 256 KB."""
        return SafeFiles(project(bridge_project_id)).read(path)

    @mcp.tool(annotations=READ)
    def search_files(bridge_project_id:str,query:str,path:str=".")->dict:
        """Literal search in approved text; no regex or shell."""
        return SafeFiles(project(bridge_project_id)).search(query,path)

    @mcp.tool(annotations=READ)
    def git_status(bridge_project_id:str)->list[str]:
        """Read filtered Git status; no caller-provided options."""
        return SafeFiles(project(bridge_project_id)).git("status")

    @mcp.tool(annotations=READ)
    def git_diff(bridge_project_id:str)->dict:
        """Read safe tracked-file changes against HEAD; no external diff/textconv."""
        return SafeFiles(project(bridge_project_id)).git("diff")

    @mcp.tool(annotations=WRITE)
    def submit_task(bridge_project_id:str,title:TaskTitle,instructions:TaskInstructions,acceptance:TaskAcceptance,idempotency_key:TaskKey)->dict:
        """Enqueue the complete plain-text Architect task; writes only queue metadata.

        Limits are Unicode characters, not UTF-8 bytes. Preserve the full specification
        and each acceptance criterion. No gzip/xz/base64 decoding is performed.
        Retry the same unchanged packet with the same idempotency key after a timeout.
        """
        project(bridge_project_id)
        return queues[bridge_project_id].submit(title,instructions,acceptance,idempotency_key)

    @mcp.tool(annotations=READ)
    def task_result(bridge_project_id:str,task_id:str)->dict:
        """Read task, executor result and revision history for independent review."""
        project(bridge_project_id)
        return queues[bridge_project_id].details(task_id)

    @mcp.tool(annotations=WRITE)
    def review_task(bridge_project_id:str,task_id:str,revision:int,verdict:str,feedback:str,idempotency_key:str)->dict:
        """Review exact result revision: ACCEPT unlocks next task; FIX queues corrected revision."""
        project(bridge_project_id)
        return queues[bridge_project_id].review(task_id,revision,verdict,feedback,idempotency_key)

    @mcp.custom_route("/owner",methods=["GET","POST"])
    async def owner(request): return await oauth.owner_page(request)

    @mcp.custom_route("/health",methods=["GET"])
    async def health(request): return JSONResponse({"service":"ai-bridge","status":"ok"})

    @mcp.custom_route("/.well-known/oauth-authorization-server/",methods=["GET"])
    async def metadata_with_trailing_slash(request):
        # Tunnel discovery preserves the issuer's trailing slash. Serve the
        # SDK metadata directly so its restricted HTTP relay needs no redirect.
        auth=mcp.settings.auth
        metadata=build_metadata(auth.issuer_url,auth.service_documentation_url,
                                auth.client_registration_options,auth.revocation_options)
        return JSONResponse(metadata.model_dump(mode="json",exclude_none=True),
                            headers={"Cache-Control":"no-store"})
    return mcp,oauth

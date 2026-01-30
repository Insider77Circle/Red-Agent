from typing import Optional

from pydantic import BaseModel, Field

from agent.tools import ToolRegistry
from proxy.pool import ProxyPool
from security.dns_leak import _test_dns_leak
from security.tls_check import (
    _get_cert_fingerprint_direct,
    _get_cert_fingerprint_via_proxy,
    _check_content_injection,
)


# --- Tool parameter models ---

class FullAuditParams(BaseModel):
    proxy_id: Optional[str] = Field(default=None, description="Proxy ID to audit")
    host: Optional[str] = Field(default=None, description="Proxy host (use with port if no proxy_id)")
    port: Optional[int] = Field(default=None, description="Proxy port (use with host)")
    target_host: str = Field(default="www.google.com", description="Target host for TLS fingerprint comparison")


# --- Tool registration ---

def register_analyzer_tools(registry: ToolRegistry):

    @registry.register(
        name="full_security_audit",
        description="Run a comprehensive security audit on a proxy: DNS leak test, TLS fingerprint check, and content injection detection.",
        parameters_model=FullAuditParams,
    )
    async def full_security_audit(
        proxy_id: str | None = None,
        host: str | None = None,
        port: int | None = None,
        target_host: str = "www.google.com",
    ) -> dict:
        pool = ProxyPool.load()
        proxy = None
        if proxy_id:
            proxy = pool.get_by_id(proxy_id)
        elif host and port:
            proxy = pool.get_by_address(host, port)

        if not proxy:
            return {"status": "not_found", "message": "Proxy not found in pool"}

        audit = {
            "proxy": proxy.to_display_dict(),
            "dns_leak": None,
            "tls_fingerprint": None,
            "content_injection": None,
            "overall_verdict": "PENDING",
            "issues": [],
        }

        # 1. DNS Leak Test
        try:
            dns_result = await _test_dns_leak(proxy)
            audit["dns_leak"] = {
                "leak_detected": dns_result.get("leak_detected"),
                "message": dns_result.get("message", ""),
                "proxy_external_ip": dns_result.get("proxy_external_ip"),
            }
            if dns_result.get("leak_detected"):
                audit["issues"].append("DNS leak detected")
        except Exception as e:
            audit["dns_leak"] = {"error": str(e)[:200]}

        # 2. TLS Fingerprint Check
        try:
            direct = _get_cert_fingerprint_direct(target_host)
            via_proxy = await _get_cert_fingerprint_via_proxy(proxy, target_host)

            if direct.get("fingerprint") and via_proxy.get("fingerprint"):
                match = direct["fingerprint"] == via_proxy["fingerprint"]
                audit["tls_fingerprint"] = {
                    "match": match,
                    "verdict": "TLS_INTACT" if match else "TLS_INTERCEPTION_DETECTED",
                    "target": target_host,
                }
                if not match:
                    audit["issues"].append("TLS interception detected")
            else:
                audit["tls_fingerprint"] = {
                    "verdict": "INCONCLUSIVE",
                    "direct_error": direct.get("error"),
                    "proxy_error": via_proxy.get("error"),
                }
        except Exception as e:
            audit["tls_fingerprint"] = {"error": str(e)[:200]}

        # 3. Content Injection Check
        try:
            injection_result = await _check_content_injection(proxy)
            audit["content_injection"] = {
                "content_modified": injection_result.get("content_modified"),
                "verdict": injection_result.get("verdict", "UNKNOWN"),
                "length_difference": injection_result.get("length_difference", 0),
            }
            if injection_result.get("content_modified"):
                audit["issues"].append("Content injection detected")
        except Exception as e:
            audit["content_injection"] = {"error": str(e)[:200]}

        # Overall verdict
        if not audit["issues"]:
            audit["overall_verdict"] = "PASS"
            audit["summary"] = "All security checks passed. This proxy appears safe for general use."
        else:
            audit["overall_verdict"] = "FAIL"
            audit["summary"] = (
                f"Security issues found: {'; '.join(audit['issues'])}. "
                "This proxy should NOT be used for sensitive traffic."
            )

        return audit

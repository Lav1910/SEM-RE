"""Print the HBM-branded report (report.html) to a US Letter PDF with headless Chrome/Chromium."""
import os
import shutil
import subprocess
import tempfile

_PF = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
CANDIDATES = [os.environ.get("HBM_CHROME", ""), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              shutil.which("chromium") or "", shutil.which("chromium-browser") or "", shutil.which("google-chrome") or ""] + \
    [os.path.join(b, *p) for b in _PF if b for p in (("Google", "Chrome", "Application", "chrome.exe"),
                                                    ("Microsoft", "Edge", "Application", "msedge.exe"))]  # Windows: Chrome or Edge


def chrome():
    for c in CANDIDATES:
        if c and os.path.exists(c):
            return c
    raise RuntimeError("Chrome or Chromium is not installed on the server")


def html_to_pdf(html_path, pdf_path, timeout=120):
    profile = tempfile.mkdtemp(prefix="hbm-pdf-")
    tmp = pdf_path + ".tmp"
    try:
        subprocess.run([chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                        f"--user-data-dir={profile}", "--no-pdf-header-footer", "--virtual-time-budget=15000",
                        "--run-all-compositor-stages-before-draw", f"--print-to-pdf={tmp}", "file://" + os.path.abspath(html_path)],
                       check=True, capture_output=True, timeout=timeout)
        if not os.path.exists(tmp) or os.path.getsize(tmp) < 1000:
            raise RuntimeError("Chrome did not produce a PDF")
        os.replace(tmp, pdf_path)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        if os.path.exists(tmp):
            os.remove(tmp)
    return pdf_path


def make_pdf(html, timeout=90):
    """PDF bytes for an HTML page, or None when this host has no PDF engine (the caller then shows a print page).
    Order: PDF_SERVICE_URL (on Vercel, the Node function at /api/pdf) -> local Chrome/Chromium/Edge."""
    import urllib.request
    svc = os.environ.get("PDF_SERVICE_URL")
    if not svc and os.environ.get("VERCEL"):
        base = os.environ.get("PUBLIC_URL") or (("https://" + os.environ["VERCEL_PROJECT_PRODUCTION_URL"])
                                               if os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") else "")
        svc = (base.rstrip("/") + "/api/pdf") if base else None
    if svc:
        secret = os.environ.get("CRON_SECRET") or os.environ.get("SECRET_KEY") or ""
        req = urllib.request.Request(svc, data=html.encode("utf-8"), method="POST",
                                     headers={"Content-Type": "text/html; charset=utf-8", "Authorization": "Bearer " + secret})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if data[:4] == b"%PDF":
                return data
        except Exception:
            pass
    try:
        chrome()
    except RuntimeError:
        return None
    d = tempfile.mkdtemp(prefix="hbm-html-")
    try:
        hp, pp = os.path.join(d, "page.html"), os.path.join(d, "page.pdf")
        with open(hp, "w", encoding="utf-8") as f:
            f.write(html)
        html_to_pdf(hp, pp)
        with open(pp, "rb") as f:
            return f.read()
    except Exception:
        return None
    finally:
        shutil.rmtree(d, ignore_errors=True)

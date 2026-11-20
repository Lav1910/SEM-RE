// Vercel Node function: prints HTML to a US Letter PDF with a serverless build of Chromium.
// Called only by the Python app (Authorization: Bearer <CRON_SECRET or SECRET_KEY>); not reachable without the secret.
const crypto = require("crypto");
const chromium = require("@sparticuz/chromium");
const puppeteer = require("puppeteer-core");

module.exports = async (req, res) => {
  const secret = process.env.CRON_SECRET || process.env.SECRET_KEY || "";
  // change 80: constant-time comparison, so the secret can't be guessed from response timing
  const want = Buffer.from(`Bearer ${secret}`), got = Buffer.from(String(req.headers.authorization || ""));
  if (!secret || want.length !== got.length || !crypto.timingSafeEqual(want, got)) { res.statusCode = 404; return res.end(); }
  if (req.method !== "POST") { res.statusCode = 405; return res.end(); }
  const chunks = [];
  let size = 0;
  for await (const c of req) { size += c.length; if (size > 8 * 1024 * 1024) { res.statusCode = 413; return res.end(); } chunks.push(c); }
  const html = Buffer.concat(chunks).toString("utf8");
  let browser;
  try {
    browser = await puppeteer.launch({ args: chromium.args, executablePath: await chromium.executablePath(), headless: true });
    const page = await browser.newPage();
    await page.setJavaScriptEnabled(false); // reports are static; nothing on them needs scripts
    // change 80: the page may load only embedded images and Google Fonts. Report text comes from scraped sites and AI
    // answers; even if something slipped through, the printer can't be pointed at internal addresses or other sites.
    await page.setRequestInterception(true);
    page.on("request", (r) => {
      const u = r.url();
      if (u.startsWith("data:") || u.startsWith("about:") || u.startsWith("https://fonts.googleapis.com/") || u.startsWith("https://fonts.gstatic.com/")) r.continue();
      else r.abort();
    });
    await page.setContent(html, { waitUntil: "networkidle0", timeout: 30000 });
    const pdf = await page.pdf({ format: "Letter", printBackground: true, preferCSSPageSize: true });
    res.setHeader("Content-Type", "application/pdf");
    res.end(Buffer.from(pdf));
  } catch (e) {
    res.statusCode = 500;
    res.end("PDF failed");
  } finally {
    if (browser) await browser.close();
  }
};

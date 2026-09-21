import { chromium } from "../admin/node_modules/playwright/index.mjs";

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
await page.goto("https://aicentre2.sligenai.cn:8443/admin/grsai-five-model-tvc.html", { waitUntil: "networkidle" });
await page.locator(".card img").first().waitFor({ state: "visible" });
await page.evaluate(async () => {
  await Promise.all([...document.images].map((image) => image.complete ? undefined : new Promise((resolve) => {
    image.addEventListener("load", resolve, { once: true });
    image.addEventListener("error", resolve, { once: true });
  })));
});
await page.screenshot({
  path: "runtime/validation/grsai-five-model-tvc-20260909/五模型TVC四类素材-完整拼图.png",
  fullPage: true,
});
await browser.close();

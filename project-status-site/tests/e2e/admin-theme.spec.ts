import { expect, test, type Page } from "@playwright/test";

const password = process.env.E2E_ADMIN_PASSWORD;

async function login(page: Page) {
  if (!password) throw new Error("E2E_ADMIN_PASSWORD is required");
  await page.goto("/admin/login");
  await page.getByLabel("管理员账号").fill("admin");
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: "进入控制台" }).click();
  await expect(page).toHaveURL(/\/admin\/overview$/);
  await expect(page.getByRole("navigation", { name: "管理后台菜单" })).toBeVisible();
}

function collectPageErrors(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    const expectedUnavailableProxy = message.text().startsWith("Failed to load resource") && message.location().url.includes("/admin/api/proxy/");
    if (message.type() === "error" && !expectedUnavailableProxy) errors.push(`console: ${message.text()}`);
  });
  return errors;
}

test("未登录访问受保护页面会进入登录页", async ({ page }) => {
  await page.goto("/admin/overview");
  await expect(page).toHaveURL(/\/admin\/login$/);
  await expect(page.getByRole("heading", { name: "登录管理后台" })).toBeVisible();
  await expect(page.getByRole("group", { name: "界面主题" })).toBeVisible();
});

test("后台使用统一的AI Centre品牌图标", async ({ page, request }) => {
  await page.goto("/admin/login");
  const icon = page.locator('link[rel="icon"]');
  await expect(icon).toHaveAttribute("href", /\/admin\/favicon\.svg\?v=3/);
  const response = await request.get("/admin/favicon.svg?v=3");
  expect(response.ok()).toBeTruthy();
  expect(await response.text()).toContain("AI Centre 2");
});

test("浅色、深色和跟随系统模式可切换并持久化", async ({ page }) => {
  await page.goto("/admin/login");

  await page.getByRole("button", { name: "深色模式" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.getByRole("button", { name: "深色模式" })).toHaveAttribute("aria-pressed", "true");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

  await page.getByRole("button", { name: "浅色模式" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

  await page.emulateMedia({ colorScheme: "dark" });
  await page.getByRole("button", { name: "跟随系统" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme-mode", "system");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.emulateMedia({ colorScheme: "light" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
});

test("登录后主题、菜单和关键页面正常工作", async ({ page }, testInfo) => {
  const errors = collectPageErrors(page);
  await login(page);

  await page.getByRole("button", { name: "深色模式" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  if (testInfo.project.name.includes("mobile")) await page.getByRole("button", { name: "打开导航" }).click();
  await page.getByRole("button", { name: "生产任务" }).click();
  await page.getByRole("button", { name: "音频" }).click();
  await page.getByRole("link", { name: /语音合成/ }).click();
  await expect(page).toHaveURL(/\/admin\/tts$/);
  await expect(page.getByRole("heading", { name: "语音合成" })).toBeVisible();
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

  if (testInfo.project.name.includes("mobile")) await page.getByRole("button", { name: "打开导航" }).click();
  await page.getByRole("button", { name: "项目管理" }).click();
  await page.getByRole("link", { name: /项目状态/ }).click();
  await expect(page).toHaveURL(/\/admin\/project$/);
  await expect(page.getByRole("heading", { name: "项目开发情况" })).toBeVisible();

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.screenshot({ path: testInfo.outputPath("admin-dark.png"), fullPage: true });

  let submittedBody: Record<string, unknown> | null = null;
  await page.route("**/admin/api/proxy/control/v1/video-generations/minimax-h3/jobs", async (route) => {
    submittedBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ job_id: "7c39cf01-9893-436e-9378-1be045d98f64", status: "queued" }),
    });
  });
  await page.goto("/admin/h3");
  const referenceGroups = page.locator(".url-list");
  const references = [
    ["https://storage.example.com/video-1.mp4", "https://storage.example.com/video-2.mp4"],
    ["https://storage.example.com/image-1.png", "https://storage.example.com/image-2.png"],
    ["https://storage.example.com/audio-1.mp3", "https://storage.example.com/audio-2.mp3"],
  ];
  for (let groupIndex = 0; groupIndex < references.length; groupIndex += 1) {
    const group = referenceGroups.nth(groupIndex);
    await group.locator("input").first().fill(references[groupIndex][0]);
    await group.getByRole("button", { name: /添加一项/ }).click();
    await group.locator("input").nth(1).fill(references[groupIndex][1]);
  }
  await page.getByLabel("第一段完整Prompt").fill("人物看向镜头并自然挥手");
  await page.getByRole("button", { name: "提交异步任务" }).click();
  await expect.poll(() => submittedBody).not.toBeNull();
  const submitted = submittedBody as Record<string, unknown> | null;
  expect(submitted?.reference_video_urls).toEqual(references[0]);
  expect(submitted?.reference_image_urls).toEqual(references[1]);
  expect(submitted?.reference_audio_urls).toEqual(references[2]);
  expect(submitted?.prompts).toEqual(["人物看向镜头并自然挥手"]);

  expect(errors).toEqual([]);
});

test("浅色后台在目标尺寸下没有横向溢出", async ({ page }, testInfo) => {
  const errors = collectPageErrors(page);
  await login(page);
  await page.getByRole("button", { name: "浅色模式" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  if (testInfo.project.name.includes("mobile")) {
    await page.getByRole("button", { name: "打开导航" }).click();
    await expect(page.locator(".sidebar")).toHaveClass(/is-mobile-open/);
    await page.getByRole("button", { name: "关闭导航" }).click({ position: { x: 360, y: 20 } });
  } else await expect(page.locator(".sidebar")).toBeVisible();
  await expect(page.locator(".panel").first()).toBeVisible();

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.screenshot({ path: testInfo.outputPath("admin-light.png"), fullPage: true });
  expect(errors).toEqual([]);
});

test("统一下拉组件支持受控选择、选中态和深浅主题", async ({ page }) => {
  await login(page);
  await page.goto("/admin/h3");

  const modeField = page.locator("label", { hasText: "分段模式" });
  await modeField.locator(".app-select-trigger").click();
  await expect(modeField.locator(".app-select-menu")).toBeVisible();
  await modeField.getByRole("option", { name: "两段" }).click();
  await expect(modeField.locator(".app-select-trigger")).toContainText("两段");
  await expect(page.getByLabel("第二段完整Prompt")).toBeVisible();

  const resolutionField = page.locator("label", { hasText: "清晰度" });
  await resolutionField.locator(".app-select-trigger").click();
  await resolutionField.getByRole("option", { name: "720P" }).click();
  await expect(resolutionField.locator(".app-select-trigger")).toContainText("720P");

  await page.getByRole("button", { name: "深色模式" }).click();
  await resolutionField.locator(".app-select-trigger").click();
  await expect(resolutionField.locator(".app-select-menu")).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});

test("筛选下拉菜单可以跨越相邻卡片并正常选择", async ({ page }) => {
  await login(page);
  await page.goto("/admin/calls");
  const statusField = page.locator(".record-filters label", { hasText: "状态" });
  await statusField.locator(".app-select-trigger").click();
  const failedOption = statusField.getByRole("option", { name: "失败" });
  await expect(failedOption).toBeVisible();
  await failedOption.click();
  await expect(statusField.locator(".app-select-trigger")).toContainText("失败");
});

test("H3弹性池可以选择机器并保存扩缩容策略", async ({ page }) => {
  let workerRequest: Record<string, unknown> | null = null;
  let policyRequest: Record<string, unknown> | null = null;
  let deletedDeployment = "";
  await login(page);
  await page.route("**/admin/api/proxy/control/internal/admin/h3/**", async route => {
    const url = route.request().url();
    if (url.endsWith("/internal/admin/h3/pool/workers")) workerRequest = route.request().postDataJSON();
    if (url.endsWith("/internal/admin/h3/pool/config")) policyRequest = route.request().postDataJSON();
    if (route.request().method() === "DELETE" && url.includes("/internal/admin/h3/pool/workers/")) deletedDeployment = url.split("/").at(-1) || "";
    const body = url.includes("/internal/admin/h3/pool/deployments?")
      ? { items: [{ id: "22222222-2222-4222-8222-222222222222", name: "h3-failed-hidden", machine_type: "5090_32g", provider_mode: "spot", status: "failed", created_at: "2026-09-03T00:00:00Z", last_error: "C004: 余额不足", available: false }], total: 491, page: 1, page_size: 20 }
      : url.endsWith("/internal/admin/h3/pool")
      ? { configured: true, capacity: 1, provider_slots: 1, pending_jobs: 0, unavailable_total: 491, unavailable_counts: { failed: 491 }, notifications: { configured: true, channel: "lark_webhook", last_status: "alert_sent" }, performance: { effective_seconds: { n: 18, mean: 620, p50: 600, p95: 710, min: 500, max: 720 }, raw_effective_seconds: { n: 19, mean: 3600, p50: 610, p95: 7000, min: 500, max: 58000 }, queue_wait_seconds: { n: 19, mean: 20, p50: 10, p95: 50, min: 0, max: 60 }, end_to_end_seconds: { n: 19, mean: 3800, p50: 650, p95: 7200, min: 520, max: 59000 }, sample_count: 19, effective_sample_count: 18, outlier_count: 1, by_machine_type: { "5090_32g": { effective_seconds: { n: 18, mean: 620, p50: 600, p95: 710, min: 500, max: 720 }, success_count: 19, outlier_count: 1 } } }, config: { min_workers: 1, max_workers: 5, idle_timeout_seconds: 1800, default_machine_type: "5090_32g", provider_mode: "spot", spot_estimated_exec_seconds: 82800, autoscaling_enabled: true, updated_at: "2026-09-03T00:00:00Z" }, deployments: [{ id: "11111111-1111-4111-8111-111111111111", provider_task_id: 3061675, name: "h3-pool-test", machine_type: "5090_32g", provider_mode: "spot", status: "active", worker_status: "online", available: true, idle_since: null, last_error: null, created_at: "2026-09-03T00:00:00Z" }], machine_types: [], provider_modes: [] }
      : url.includes("/internal/admin/h3/jobs") ? { items: [], total: 0 }
      : url.endsWith("/internal/admin/h3/workers") ? { workers: [{ id: "33333333-3333-4333-8333-333333333333", name: "h3-online", base_url: "https://job***", status: "online", enabled: true, draining: false, queue_running: 0, queue_pending: 0, last_checked_at: "2026-09-03T00:00:00Z", status_duration_seconds: 321 }, { id: "44444444-4444-4444-8444-444444444444", name: "h3-offline-hidden", base_url: "https://job***", status: "offline", enabled: true, draining: false, queue_running: 0, queue_pending: 0, last_checked_at: "2026-09-03T00:00:00Z", offline_duration_seconds: 1900, status_duration_seconds: 1900 }], summary: { total: 2, online: 1, idle: 1, busy: 0, offline: 1, disabled: 0, draining: 0, incompatible: 0, unknown: 0 } }
      : { accepted: true };
    await route.fulfill({ status: url.endsWith("/pool/workers") ? 202 : 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/admin/h3");
  await expect(page.getByText("h3-online", { exact: true })).toBeVisible();
  await expect(page.getByText("当前状态持续", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "5m 21s 持续在线" })).toBeVisible();
  await expect(page.getByText("h3-offline-hidden", { exact: true })).toBeHidden();
  await expect(page.getByText("h3-failed-hidden", { exact: true })).toBeHidden();
  await page.getByText(/暂不可用（Worker 1/).click();
  await expect(page.getByText("h3-offline-hidden", { exact: true })).toBeVisible();
  await expect(page.getByText("31m 40s", { exact: true })).toBeVisible();
  await expect(page.getByText("h3-failed-hidden", { exact: true })).toBeVisible();
  await expect(page.getByText("第 1 / 25 页，共 491 条")).toBeVisible();

  const createPanel = page.locator(".panel", { hasText: "共绩 Worker" });
  const machineField = createPanel.locator("label", { hasText: "机器类型" });
  await machineField.locator(".app-select-trigger").click();
  await machineField.getByRole("option", { name: "RTX 4090 · 48G" }).click();
  const providerField = createPanel.locator("label", { hasText: "供应方式" });
  await providerField.locator(".app-select-trigger").click();
  await providerField.getByRole("option", { name: "抢占式 Spot Job（默认）" }).click();
  await createPanel.getByRole("button", { name: "增加 Worker" }).click();
  await expect.poll(() => workerRequest).not.toBeNull();
  expect((workerRequest as Record<string, unknown> | null)?.machine_type).toBe("4090_48g");
  expect((workerRequest as Record<string, unknown> | null)?.provider_mode).toBe("spot");

  const policyPanel = page.locator(".panel", { hasText: "调度策略" });
  await policyPanel.getByLabel("最小 Worker 数").fill("2");
  await policyPanel.getByLabel("最大 Worker 数").fill("6");
  await policyPanel.getByLabel("空闲最大存活时间（分钟）").fill("45");
  await policyPanel.getByRole("button", { name: "保存策略" }).click();
  await expect.poll(() => policyRequest).not.toBeNull();
  expect(policyRequest).toMatchObject({ min_workers: 2, max_workers: 6, idle_timeout_seconds: 2700, provider_mode: "spot", spot_estimated_exec_seconds: 82800 });

  await page.getByRole("button", { name: "永久停止" }).click();
  const dialog = page.getByRole("dialog", { name: "永久停止共绩实例" });
  await expect(dialog).toContainText("停止后不可恢复");
  await dialog.getByRole("button", { name: "永久停止" }).click();
  await expect.poll(() => deletedDeployment).toBe("11111111-1111-4111-8111-111111111111");
});

test("调用与任务使用服务端分页且关联调用最多显示十条", async ({ page }) => {
  await login(page);
  const requestedPages: string[] = [];
  const taskId = "task-internal-1";
  const jobId = "job-external-1";
  const calls = Array.from({ length: 20 }, (_, index) => ({
    id: `call-${index + 1}`,
    trace_id: `trace-${index + 1}`,
    started_at: "2026-09-02T10:00:00+00:00",
    service: "h3",
    operation: "status",
    method: "GET",
    path: `/v1/video-generations/minimax-h3/jobs/${jobId}`,
    status_code: 200,
    success: true,
    duration_ms: 12,
    task_id: taskId,
  }));
  const task = {
    id: taskId,
    external_task_id: jobId,
    service: "h3",
    operation: "generate",
    status: "running",
    stage: "generating_part_1",
    created_at: "2026-09-02T10:00:00+00:00",
    updated_at: "2026-09-02T10:00:00+00:00",
    input_quantity: "0",
    unit_type: "task",
    billing_status: "unpriced",
  };

  await page.route("**/admin/api/proxy/control/internal/admin/observability/calls?*", async (route) => {
    const url = new URL(route.request().url());
    requestedPages.push(`calls:${url.searchParams.get("page")}:${url.searchParams.get("page_size")}`);
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: calls, total: 57, page: Number(url.searchParams.get("page")), page_size: Number(url.searchParams.get("page_size")) }) });
  });
  await page.route("**/admin/api/proxy/control/internal/admin/observability/tasks?*", async (route) => {
    const url = new URL(route.request().url());
    requestedPages.push(`tasks:${url.searchParams.get("page")}:${url.searchParams.get("page_size")}`);
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [task], total: 1, page: 1, page_size: Number(url.searchParams.get("page_size")) }) });
  });
  await page.route(`**/admin/api/proxy/control/internal/admin/observability/tasks/${taskId}`, async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...task, calls: calls.slice(0, 10), calls_total: 57, events: [] }) });
  });

  await page.goto("/admin/calls");
  await expect(page.getByText("第 1 / 3 页，共 57 条")).toBeVisible();
  await page.getByRole("button", { name: "下一页" }).click();
  await expect.poll(() => requestedPages).toContain("calls:2:20");
  await page.getByRole("button", { name: /业务任务日志/ }).click();
  await expect.poll(() => requestedPages).toContain("tasks:1:20");
  await page.getByRole("button", { name: "查看" }).click();
  await expect(page.locator(".linked-calls button")).toHaveCount(10);
  await expect(page.getByText("仅显示最近10条精确关联调用，共 57 条。")).toBeVisible();
});

test("真实数据看板区分API与任务耗时并支持H3机型筛选", async ({ page }) => {
  await login(page);
  const requestedMachines: Array<string | null> = [];
  await page.route("**/admin/api/proxy/control/internal/admin/observability/analytics/**", async route => {
    const url = new URL(route.request().url());
    const metric = (n: number, mean: number | null, p50: number | null, p95: number | null) => ({ n, mean, p50, p95, p99: p95, min: p50, max: p95 });
    let body: unknown;
    if (url.pathname.endsWith("/summary")) body = {
      range: { from: "2026-09-01T00:00:00Z", to: "2026-09-08T00:00:00Z", timezone: "Asia/Shanghai" }, collection_started_at: "2026-09-01T00:00:00Z",
      api: { calls: 120, successes: 118, success_rate: 118 / 120, latency_ms: metric(120, 80, 60, 190) },
      tasks: { total: 40, terminal: 38, succeeded: 37, failed: 1, success_rate: 37 / 38, latency_ms: metric(37, 320000, 280000, 620000), statuses: { succeeded: 37, failed: 1, running: 2 } },
      usage: [{ unit_type: "video_minute", quantity: 6.5 }], billing: { amount: 12.5, waste_amount: 0.4, unpriced_tasks: 0 },
      endpoints: [{ service: "h3", operation: "generate", calls: 40, call_successes: 40, call_success_rate: 1, tasks: 40, terminal_tasks: 38, task_successes: 37, task_success_rate: 37 / 38, api_latency_ms: metric(40, 40, 35, 75), task_latency_ms: metric(37, 320000, 280000, 620000), last_error: null }], errors: [],
    };
    else if (url.pathname.endsWith("/timeseries")) body = { points: [{ day: "2026-09-07", calls: 20, call_successes: 20, tasks: 8, task_successes: 7, task_failures: 1, api_latency_ms: metric(20, 80, 60, 190), task_latency_ms: metric(7, 320000, 280000, 620000) }] };
    else if (url.pathname.endsWith("/stages")) body = { stages: [{ service: "h3", stage: "end_to_end", label: "端到端", latency_ms: metric(37, 320000, 280000, 620000), source: "measured" }], note: "真实任务" };
    else {
      requestedMachines.push(url.searchParams.get("machine_type"));
      const emptyDuration = (duration_seconds: number) => ({ duration_seconds, end_to_end: metric(0, null, null, null), requested_duration: metric(0, null, null, null), output_duration: metric(0, null, null, null), stages: { generation: metric(0, null, null, null) } });
      const durations = Array.from({ length: 12 }, (_, index) => emptyDuration(index + 4));
      durations[1] = { ...durations[1], end_to_end: metric(9, 310, 290, 590), stages: { generation: metric(9, 270, 250, 520) } };
      body = { jobs: 10, terminal_jobs: 9, succeeded_jobs: 9, failed_jobs: 0, success_rate: 1, retry_jobs: 1, retry_rate: 0.1, generated_seconds_per_processing_hour: 58.1, coverage: { measured: 2, estimated: 7 }, submission_latency_ms: metric(10, 45, 40, 80), overall: { end_to_end: metric(9, 310, 290, 590), requested_duration: metric(9, 5, 5, 5), output_duration: metric(9, 5, 5, 5), stages: { generation: metric(9, 270, 250, 520) } }, durations, by_machine_type: { "4090_24g": { end_to_end: metric(9, 310, 290, 590), requested_duration: metric(9, 5, 5, 5), output_duration: metric(9, 5, 5, 5), stages: { generation: metric(9, 270, 250, 520) } } } };
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.route("**/admin/api/proxy/control/internal/admin/observability/tasks?*", route => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [], total: 0 }) }));
  await page.goto("/admin/overview");
  await expect(page.getByText("API响应 P95")).toBeVisible();
  await expect(page.getByText("任务端到端 P95", { exact: true })).toBeVisible();
  await expect(page.getByText("4～15秒分阶段性能")).toBeVisible();
  await expect(
    page.locator(".h3-analytics-section table").last().getByRole("cell", { name: "5s", exact: true }),
  ).toBeVisible();
  const machine = page.locator(".h3-analytics-section label", { hasText: "GPU机型" });
  await machine.locator(".app-select-trigger").click();
  await machine.getByRole("option", { name: "RTX 4090 · 24G" }).click();
  await expect.poll(() => requestedMachines).toContain("4090_24g");
});

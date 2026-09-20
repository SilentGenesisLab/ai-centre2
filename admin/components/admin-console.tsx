"use client";

import Link from "next/link";
import { ThemeToggle } from "@/components/theme-toggle";
import { FormEvent, ReactNode, useCallback, useEffect, useState } from "react";
import { AppSelect as Select } from "@/components/ui/app-select";
import {
  NAVIGATION,
  SECTIONS,
  type NavigationItem,
  type SectionId,
} from "@/lib/sections";

const BASE_PATH = "/admin";

type ServiceState = {
  name: string;
  ActiveState: string;
  SubState: string;
  UnitFileState: string;
};

type GpuState = {
  gpu_id: number;
  enabled: boolean;
  services: ServiceState[];
};

type Health = {
  status: string;
  upstreams?: Record<
    string,
    { status: string; url?: string; details?: Record<string, unknown> }
  >;
  gpus?: GpuState[];
};

type HealthMonitorItem = {
  id: string;
  name: string;
  current_status: string;
  last_checked_at?: string | null;
  last_latency_ms?: number | null;
  last_l2_status?: string | null;
  last_l2_at?: string | null;
  consecutive_failures: number;
  availability?: number | null;
  p50_ms?: number | null;
  p95_ms?: number | null;
  sample_count: number;
  history: Array<{ at: string; ok: boolean; level: string }>;
};
type HealthNotificationConfig = {
  enabled: boolean;
  configured: boolean;
  webhook_host?: string | null;
  updated_at?: string | null;
  last_delivery_at?: string | null;
  last_delivery_status?: string | null;
};

type LipSyncJob = {
  job_id: string;
  state: string;
  stage: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  video_filename?: string;
  audio_filename?: string;
  face_restore?: boolean;
  elapsed_seconds?: number;
  musetalk_seconds?: number;
  gfpgan_seconds?: number;
  error?: string | null;
};

type VoiceProfile = {
  voice_profile_id: string;
  display_name: string;
  languages: string[];
  bindings: Record<string, unknown>;
  fallback_order: string[];
  version: number;
};

type AuditEvent = {
  id: string;
  timestamp: string;
  username: string;
  address: string;
  action: string;
  resource: string;
  outcome: string;
  status?: number;
};

type TtsJob = {
  job_id: string;
  status: string;
  duplicate?: boolean;
  result?: Record<string, unknown> | null;
  error?: string | null;
};

type ObservabilityOverview = {
  range: { from: string; to: string; timezone: string };
  collection_started_at?: string | null;
  calls: number;
  call_successes: number;
  call_success_rate: number | null;
  task_success_rate: number | null;
  task_statuses: Record<string, number>;
  average_ms: number;
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
  amount: number;
  waste_amount: number;
  unpriced_tasks: number;
  services: Array<{
    service: string;
    calls: number;
    successes: number;
    average_ms: number;
  }>;
  trend: Array<{
    day: string;
    service: string;
    calls: number;
    successes: number;
    average_ms: number;
  }>;
  task_trend: Array<{
    day: string;
    service: string;
    tasks: number;
    successes: number;
    failures: number;
  }>;
  billing_trend: Array<{
    day: string;
    service: string;
    amount: number;
    waste_amount: number;
  }>;
  errors: Array<{ error_code: string; count: number }>;
};

type MetricSummary = {
  n: number;
  mean: number | null;
  p50: number | null;
  p95: number | null;
  p99?: number | null;
  min?: number | null;
  max?: number | null;
};

type AnalyticsSummary = {
  range: { from: string; to: string; timezone: string };
  collection_started_at?: string | null;
  api: {
    calls: number;
    successes: number;
    success_rate: number | null;
    latency_ms: MetricSummary;
  };
  tasks: {
    total: number;
    terminal: number;
    succeeded: number;
    failed: number;
    success_rate: number | null;
    latency_ms: MetricSummary;
    statuses: Record<string, number>;
  };
  usage: Array<{ unit_type: string; quantity: number }>;
  billing: { amount: number; waste_amount: number; unpriced_tasks: number };
  endpoints: Array<{
    service: string;
    operation: string;
    calls: number;
    call_successes: number;
    call_success_rate: number | null;
    tasks: number;
    terminal_tasks: number;
    task_successes: number;
    task_success_rate: number | null;
    api_latency_ms: MetricSummary;
    task_latency_ms: MetricSummary;
    last_error?: string | null;
  }>;
  errors: Array<{ error_code: string; count: number }>;
};

type AnalyticsTimeseries = {
  points: Array<{
    day: string;
    calls: number;
    call_successes: number;
    tasks: number;
    task_successes: number;
    task_failures: number;
    api_latency_ms: MetricSummary;
    task_latency_ms: MetricSummary;
  }>;
};

type AnalyticsStages = {
  stages: Array<{
    service: string;
    stage: string;
    label: string;
    latency_ms: MetricSummary;
    source: string;
  }>;
  note?: string;
};

type H3DurationAnalytics = {
  jobs: number;
  terminal_jobs: number;
  succeeded_jobs: number;
  failed_jobs: number;
  success_rate: number | null;
  retry_jobs: number;
  retry_rate: number | null;
  generated_seconds_per_processing_hour: number | null;
  coverage: { measured: number; estimated: number };
  submission_latency_ms: MetricSummary;
  overall: {
    end_to_end: MetricSummary;
    requested_duration: MetricSummary;
    output_duration: MetricSummary;
    stages: Record<string, MetricSummary>;
  };
  durations: Array<{
    duration_seconds: number;
    end_to_end: MetricSummary;
    requested_duration: MetricSummary;
    output_duration: MetricSummary;
    stages: Record<string, MetricSummary>;
  }>;
  by_machine_type: Record<
    string,
    {
      end_to_end: MetricSummary;
      requested_duration: MetricSummary;
      output_duration: MetricSummary;
      stages: Record<string, MetricSummary>;
    }
  >;
};

type ApiCallRecord = {
  id: string;
  trace_id: string;
  started_at: string;
  service: string;
  operation: string;
  method: string;
  path: string;
  status_code: number;
  success: boolean;
  duration_ms: number;
  task_id?: string | null;
  error_code?: string | null;
  request_summary?: unknown;
  response_summary?: unknown;
  snapshots?: Array<{ kind: string; truncated: number; expires_at: string }>;
};

type UnifiedTask = {
  id: string;
  external_task_id?: string | null;
  service: string;
  operation: string;
  status: string;
  stage: string;
  created_at: string;
  updated_at: string;
  duration_ms?: number | null;
  input_quantity: string;
  unit_type: string;
  amount?: string | null;
  waste_amount?: string | null;
  billing_status?: string | null;
  result_summary?: unknown;
  events?: Array<Record<string, unknown>>;
  calls?: Array<Record<string, unknown>>;
  calls_total?: number;
  billing?: Record<string, unknown> | null;
};

type PricingRule = {
  id: string;
  service: string;
  operation: string;
  unit_type: string;
  currency: string;
  fixed_fee: string;
  unit_price: string;
  effective_from: string;
  enabled: boolean;
};

type H3Job = {
  id: string;
  job_id: string;
  status: string;
  stage: string;
  progress: number;
  priority: number;
  attempt_count: number;
  created_at: string;
  result_url?: string | null;
  part_urls?: string[];
  error?: string | null;
  elapsed_seconds?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  request?: Record<string, unknown>;
  output?: Record<string, unknown> | null;
  attempts?: Array<Record<string, unknown>>;
  timing?: {
    queue_wait_seconds?: number | null;
    effective_processing_seconds?: number | null;
    render_and_upload_seconds?: number | null;
    end_to_end_seconds?: number | null;
    stage_seconds?: Record<string, number>;
  };
};

type H3WorkerSummary = {
  total: number;
  online: number;
  idle: number;
  busy: number;
  offline: number;
  disabled: number;
  draining: number;
  incompatible: number;
  unknown: number;
};

type H3Worker = {
  id: string;
  name: string;
  base_url: string;
  status: string;
  enabled: boolean;
  draining: boolean;
  gpu_name?: string | null;
  vram_total_bytes?: number | null;
  vram_free_bytes?: number | null;
  queue_running: number;
  queue_pending: number;
  current_job_id?: string | null;
  last_error?: string | null;
  last_checked_at?: string | null;
  offline_since?: string | null;
  offline_duration_seconds?: number | null;
  status_since?: string | null;
  status_duration_seconds?: number | null;
};

type H3PoolConfig = {
  min_workers: number;
  max_workers: number;
  idle_timeout_seconds: number;
  default_machine_type: "4090_24g" | "4090_48g" | "5090_32g";
  provider_mode: "spot" | "deployment";
  spot_estimated_exec_seconds: number;
  autoscaling_enabled: boolean;
  updated_at: string;
};

type H3ManagedDeployment = {
  id: string;
  provider_task_id?: number | null;
  worker_id?: string | null;
  name: string;
  machine_type: string;
  provider_mode: "spot" | "deployment";
  status: string;
  idle_since?: string | null;
  last_error?: string | null;
  created_at: string;
  available?: boolean;
  worker_status?: string | null;
  offline_duration_seconds?: number | null;
};

type H3MetricSummary = {
  n: number;
  min: number | null;
  mean: number | null;
  p50: number | null;
  p95: number | null;
  max: number | null;
};

type H3PoolStatus = {
  configured: boolean;
  config: H3PoolConfig;
  capacity: number;
  provider_slots: number;
  pending_jobs: number;
  deployments: H3ManagedDeployment[];
  unavailable_total: number;
  unavailable_counts: Record<string, number>;
  performance: {
    effective_seconds: H3MetricSummary;
    raw_effective_seconds: H3MetricSummary;
    queue_wait_seconds: H3MetricSummary;
    end_to_end_seconds: H3MetricSummary;
    sample_count: number;
    effective_sample_count: number;
    outlier_count: number;
    by_machine_type: Record<
      string,
      {
        effective_seconds: H3MetricSummary;
        success_count: number;
        outlier_count: number;
      }
    >;
  };
  notifications: {
    configured: boolean;
    channel: string;
    last_status?: string | null;
    last_at?: string | null;
    last_error?: string | null;
  };
  machine_types: Array<{ value: string; label: string }>;
  provider_modes: Array<{ value: "spot" | "deployment"; label: string }>;
};

type ManagedApiKey = {
  id: string;
  name: string;
  prefix: string;
  quota: number | null;
  used: number;
  remaining: number | null;
  expires_at: string | null;
  enabled: boolean;
  created_at: string;
  last_used_at: string | null;
  api_key?: string;
};

const BUSINESS_SECTIONS: Partial<Record<SectionId, string>> = {
  lipsync: "lipsync",
  asr: "asr",
  tts: "tts",
  ocr: "ocr",
  face: "face",
  scene: "scene",
  depth: "depth",
  separation: "separation",
  h3: "h3",
};

function dateInput(value: Date): string {
  const local = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

const UI_TODAY = dateInput(new Date());
const UI_SEVEN_DAYS_AGO = dateInput(new Date(Date.now() - 6 * 86_400_000));
const UI_THIRTY_DAYS_AGO = dateInput(new Date(Date.now() - 29 * 86_400_000));

function percent(value: number | null | undefined): string {
  return value === null || value === undefined
    ? "—"
    : `${(value * 100).toFixed(1)}%`;
}

function money(value: number | string | null | undefined): string {
  const number = Number(value || 0);
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    minimumFractionDigits: 2,
  }).format(number);
}

function classNames(...values: Array<string | false | null | undefined>) {
  return values.filter(Boolean).join(" ");
}

function formatTime(value?: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatDuration(value?: number) {
  if (value === undefined || value === null) return "—";
  if (!Number.isFinite(value) || value < 0) return "—";
  const totalSeconds = Math.round(value);
  if (totalSeconds < 1) return "<1s";
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

function formatDurationMs(value?: number | null) {
  return value === undefined || value === null
    ? "—"
    : formatDuration(value / 1000);
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function formNumber(data: FormData, name: string, fallback: number): number {
  const value = Number(data.get(name));
  return Number.isFinite(value) ? value : fallback;
}

function StatusBadge({ status }: { status: string }) {
  const normalized = status.toLowerCase();
  const good = [
    "ok",
    "active",
    "running",
    "completed",
    "succeeded",
    "enabled",
  ].includes(normalized);
  const bad = ["failed", "error", "degraded", "inactive", "disabled"].includes(
    normalized,
  );
  return (
    <span
      className={classNames("status-badge", good && "is-good", bad && "is-bad")}
    >
      {status}
    </span>
  );
}

function PageHeading({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <header className="page-heading">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

function Panel({
  title,
  eyebrow,
  children,
  className,
}: {
  title: string;
  eyebrow?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={classNames("panel", className)}>
      <div className="panel-heading">
        <div>
          {eyebrow && <p className="eyebrow">{eyebrow}</p>}
          <h2>{title}</h2>
        </div>
      </div>
      {children}
    </section>
  );
}

function Modal({
  title,
  children,
  onClose,
  footer,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  footer?: ReactNode;
}) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="app-modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <h2>{title}</h2>
          <button type="button" onClick={onClose} aria-label="关闭弹窗">
            ×
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer && <footer>{footer}</footer>}
      </section>
    </div>
  );
}

function ConfirmModal({
  title,
  message,
  confirmLabel = "确认",
  busy,
  onConfirm,
  onClose,
}: {
  title: string;
  message: string;
  confirmLabel?: string;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Modal
      title={title}
      onClose={onClose}
      footer={
        <>
          <button className="secondary-button" type="button" onClick={onClose}>
            取消
          </button>
          <button
            className="danger-button"
            type="button"
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? "处理中…" : confirmLabel}
          </button>
        </>
      }
    >
      <p className="modal-message">{message}</p>
    </Modal>
  );
}

function UrlListInput({
  name,
  label,
  placeholder,
  required = false,
}: {
  name: string;
  label: string;
  placeholder: string;
  required?: boolean;
}) {
  const [values, setValues] = useState([""]);
  return (
    <fieldset className="url-list">
      <legend>{label}</legend>
      {values.map((value, index) => (
        <div className="url-list-row" key={index}>
          <input
            name={name}
            type="url"
            value={value}
            required={required && index === 0}
            placeholder={placeholder}
            onChange={(event) =>
              setValues((items) =>
                items.map((item, itemIndex) =>
                  itemIndex === index ? event.target.value : item,
                ),
              )
            }
          />
          <button
            type="button"
            className="icon-button"
            disabled={values.length === 1}
            onClick={() =>
              setValues((items) =>
                items.filter((_, itemIndex) => itemIndex !== index),
              )
            }
            aria-label={`删除第${index + 1}项`}
          >
            −
          </button>
        </div>
      ))}
      <button
        type="button"
        className="list-add-button"
        disabled={values.length >= 8}
        onClick={() => setValues((items) => [...items, ""])}
      >
        ＋ 添加一项
      </button>
    </fieldset>
  );
}

function NavIcon({ name }: { name: string }) {
  const paths: Record<string, ReactNode> = {
    dashboard: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="2" />
        <rect x="14" y="3" width="7" height="7" rx="2" />
        <rect x="3" y="14" width="7" height="7" rx="2" />
        <rect x="14" y="14" width="7" height="7" rx="2" />
      </>
    ),
    logs: (
      <>
        <path d="M5 4h14v16H5z" />
        <path d="M8 8h8M8 12h8M8 16h5" />
      </>
    ),
    billing: (
      <>
        <path d="M12 2v20M17 6.5H9.5a3 3 0 0 0 0 6h5a3 3 0 0 1 0 6H7" />
      </>
    ),
    key: (
      <>
        <circle cx="8" cy="15" r="4" />
        <path d="m11 12 9-9M16 7l3 3M14 9l3 3" />
      </>
    ),
    production: (
      <>
        <path d="m4 15 8-12 8 12-8 6z" />
        <path d="M8 15h8" />
      </>
    ),
    gpu: (
      <>
        <rect x="4" y="7" width="16" height="10" rx="2" />
        <path d="M8 4v3m4-3v3m4-3v3M8 17v3m4-3v3m4-3v3M1 10h3m-3 4h3m16-4h3m-3 4h3" />
      </>
    ),
    audit: (
      <>
        <path d="M12 3 4 6v6c0 5 3.4 8 8 9 4.6-1 8-4 8-9V6z" />
        <path d="m9 12 2 2 4-5" />
      </>
    ),
    project: (
      <>
        <path d="M4 7h16v13H4z" />
        <path d="M9 7V4h6v3" />
      </>
    ),
    settings: (
      <>
        <circle cx="12" cy="12" r="3" />
        <path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.4 1A8 8 0 0 0 15 6l-.3-2.6h-4L10.5 6A8 8 0 0 0 9 7.1l-2.4-1-2 3.4L6.7 11a7 7 0 0 0 0 2l-2 1.5 2 3.4 2.4-1A8 8 0 0 0 10.5 18l.3 2.6h4L15 18a8 8 0 0 0 1.5-1.1l2.4 1 2-3.4L19 13a7 7 0 0 0 0-1z" />
      </>
    ),
  };
  const fallback = (
    <>
      <circle cx="12" cy="12" r="8" />
      <path d="M8 12h8M12 8v8" />
    </>
  );
  return (
    <svg
      className="nav-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {paths[name] || fallback}
    </svg>
  );
}

function containsSection(item: NavigationItem, section: SectionId): boolean {
  return (
    item.href === section ||
    Boolean(item.children?.some((child) => containsSection(child, section)))
  );
}

function SidebarItem({
  item,
  section,
  level,
  open,
  toggle,
  closeMobile,
}: {
  item: NavigationItem;
  section: SectionId;
  level: number;
  open: Set<string>;
  toggle: (id: string) => void;
  closeMobile: () => void;
}) {
  const active = containsSection(item, section);
  const expanded = open.has(item.id);
  if (item.children)
    return (
      <div className={`nav-tree level-${level} ${active ? "has-active" : ""}`}>
        <button
          className={`nav-item nav-parent ${active ? "is-current-parent" : ""}`}
          onClick={() => toggle(item.id)}
          aria-expanded={expanded}
        >
          <NavIcon name={item.icon} />
          <span>{item.label}</span>
          <i className={`nav-chevron ${expanded ? "is-open" : ""}`}>›</i>
        </button>
        {expanded && (
          <div className="nav-children">
            {item.children.map((child) => (
              <SidebarItem
                key={child.id}
                item={child}
                section={section}
                level={level + 1}
                open={open}
                toggle={toggle}
                closeMobile={closeMobile}
              />
            ))}
          </div>
        )}
      </div>
    );
  return (
    <Link
      className={`nav-item level-${level} ${item.href === section ? "is-active" : ""} ${item.disabled ? "is-disabled" : ""}`}
      href={`/${item.href}`}
      onClick={closeMobile}
    >
      <NavIcon name={item.icon} />
      <span>{item.label}</span>
      {item.disabled && <small>规划中</small>}
    </Link>
  );
}

async function apiRequest<T>(
  backend: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`${BASE_PATH}/api/proxy/${backend}${path}`, {
    cache: "no-store",
    ...init,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (body.detail)
        detail =
          typeof body.detail === "string"
            ? body.detail
            : JSON.stringify(body.detail);
    } catch {}
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export function AdminConsole({
  section,
  username,
}: {
  section: SectionId;
  username: string;
}) {
  const [health, setHealth] = useState<Health | null>(null);
  const [jobs, setJobs] = useState<LipSyncJob[]>([]);
  const [ocrHealth, setOcrHealth] = useState<Record<string, unknown> | null>(
    null,
  );
  const [faceHealth, setFaceHealth] = useState<Record<string, unknown> | null>(
    null,
  );
  const [voices, setVoices] = useState<VoiceProfile[]>([]);
  const [providers, setProviders] = useState<Array<Record<string, unknown>>>(
    [],
  );
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const [openNav, setOpenNav] = useState<Set<string>>(() => {
    const initial = new Set<string>();
    NAVIGATION.forEach((item) => {
      if (containsSection(item, section)) {
        initial.add(item.id);
        item.children?.forEach((child) => {
          if (containsSection(child, section)) initial.add(child.id);
        });
      }
    });
    return initial;
  });

  const refresh = useCallback(async () => {
    setRefreshing(true);
    setError("");
    try {
      const [healthResult, jobsResult, ocrResult, faceResult] =
        await Promise.allSettled([
          apiRequest<Health>("control", "/health"),
          apiRequest<{ jobs: LipSyncJob[]; total: number }>(
            "control",
            "/v1/lipsync/jobs?limit=100",
          ),
          apiRequest<Record<string, unknown>>("ocr", "/health"),
          apiRequest<Record<string, unknown>>("face", "/health"),
        ]);
      if (healthResult.status === "fulfilled") setHealth(healthResult.value);
      if (jobsResult.status === "fulfilled") {
        setJobs(jobsResult.value.jobs);
      }
      if (ocrResult.status === "fulfilled") setOcrHealth(ocrResult.value);
      if (faceResult.status === "fulfilled") setFaceHealth(faceResult.value);
      const firstFailure = [healthResult, jobsResult].find(
        (result) => result.status === "rejected",
      );
      if (firstFailure?.status === "rejected") throw firstFailure.reason;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "刷新失败");
    } finally {
      setRefreshing(false);
    }
  }, []);

  const loadTts = useCallback(async () => {
    try {
      const [voiceResult, providerResult] = await Promise.all([
        apiRequest<{ voices: VoiceProfile[] }>("control", "/v2/tts/voices"),
        apiRequest<{ providers: Array<Record<string, unknown>> }>(
          "control",
          "/v2/tts/providers",
        ),
      ]);
      setVoices(voiceResult.voices);
      setProviders(providerResult.providers);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "TTS 信息加载失败");
    }
  }, []);

  const loadAudit = useCallback(async () => {
    try {
      const response = await fetch(`${BASE_PATH}/api/audit`, {
        cache: "no-store",
      });
      if (!response.ok) throw new Error("审计记录加载失败");
      setAudit(((await response.json()) as { events: AuditEvent[] }).events);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "审计记录加载失败");
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void refresh(), 0);
    return () => window.clearTimeout(timer);
  }, [refresh]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (["tts", "models"].includes(section)) void loadTts();
      if (section === "audit") void loadAudit();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [section, loadAudit, loadTts]);

  useEffect(() => {
    if (!["overview", "lipsync", "resources"].includes(section)) return;
    const timer = window.setInterval(() => void refresh(), 8000);
    return () => window.clearInterval(timer);
  }, [section, refresh]);

  const activeSection = SECTIONS.find((item) => item.id === section)!;

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const saved = window.localStorage.getItem("ai-centre-sidebar-collapsed");
      if (saved) setSidebarCollapsed(saved === "true");
      const savedOpen = window.localStorage.getItem("ai-centre-nav-open");
      if (savedOpen)
        setOpenNav(
          (previous) => new Set([...previous, ...JSON.parse(savedOpen)]),
        );
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  function toggleNav(id: string) {
    setOpenNav((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      window.localStorage.setItem(
        "ai-centre-nav-open",
        JSON.stringify([...next]),
      );
      return next;
    });
  }
  function toggleSidebar() {
    setSidebarCollapsed((value) => {
      window.localStorage.setItem(
        "ai-centre-sidebar-collapsed",
        String(!value),
      );
      return !value;
    });
  }

  function showNotice(message: string) {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 3500);
  }

  async function logout() {
    await fetch(`${BASE_PATH}/api/auth/logout`, { method: "POST" });
    window.location.assign(`${BASE_PATH}/login`);
  }

  return (
    <div
      className={`admin-layout ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}
    >
      {mobileNav && (
        <button
          className="mobile-nav-backdrop"
          aria-label="关闭导航"
          onClick={() => setMobileNav(false)}
        />
      )}
      <aside className={`sidebar ${mobileNav ? "is-mobile-open" : ""}`}>
        <div className="sidebar-brand">
          <span className="product-mark">AC</span>
          <div>
            <strong>AI Centre 2</strong>
            <small>PRODUCTION CONSOLE</small>
          </div>
          <button
            className="sidebar-collapse"
            onClick={toggleSidebar}
            aria-label="折叠侧栏"
          >
            ‹
          </button>
        </div>
        <nav className="sidebar-nav" aria-label="管理后台菜单">
          {NAVIGATION.map((item) => (
            <SidebarItem
              key={item.id}
              item={item}
              section={section}
              level={0}
              open={openNav}
              toggle={toggleNav}
              closeMobile={() => setMobileNav(false)}
            />
          ))}
        </nav>
        <div className="sidebar-foot">
          <div>
            <i className="signal signal-live" />
            <span>生产环境</span>
          </div>
          <small>8443 · HTTPS</small>
        </div>
      </aside>
      <div className="main-column">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="mobile-menu-button"
              onClick={() => setMobileNav(true)}
              aria-label="打开导航"
            >
              ☰
            </button>
            <span>AI Centre</span>
            <b>/</b>
            <strong>{activeSection.label}</strong>
          </div>
          <div className="topbar-actions">
            <ThemeToggle compact />
            <button
              className="icon-button"
              onClick={() => void refresh()}
              aria-label="刷新"
              title="刷新"
            >
              {refreshing ? "···" : "↻"}
            </button>
            <div className="account">
              <span>{username.slice(0, 1).toUpperCase()}</span>
              <div>
                <strong>{username}</strong>
                <small>系统管理员</small>
              </div>
            </div>
            <button className="text-button" onClick={() => void logout()}>
              退出
            </button>
          </div>
        </header>
        <main className="content">
          {error && (
            <div className="global-message is-error">
              <b>请求失败</b>
              <span>{error}</span>
              <button onClick={() => setError("")}>×</button>
            </div>
          )}
          {notice && (
            <div className="global-message is-success">
              <b>操作完成</b>
              <span>{notice}</span>
            </div>
          )}
          {section === "overview" && (
            <Analytics
              health={health}
              jobs={jobs}
              ocrHealth={ocrHealth}
              faceHealth={faceHealth}
              setError={setError}
            />
          )}
          {section === "calls" && <CallsAndTasks setError={setError} />}
          {section === "billing" && (
            <BillingCenter setError={setError} showNotice={showNotice} />
          )}
          {section === "lipsync" && (
            <LipSync
              jobs={jobs}
              refresh={refresh}
              showNotice={showNotice}
              setError={setError}
            />
          )}
          {section === "asr" && <Asr setError={setError} />}
          {section === "tts" && (
            <Tts voices={voices} providers={providers} setError={setError} />
          )}
          {section === "ocr" && <Ocr setError={setError} />}
          {section === "face" && (
            <Face setError={setError} showNotice={showNotice} />
          )}
          {section === "scene" && (
            <SceneDetect setError={setError} showNotice={showNotice} />
          )}
          {section === "depth" && (
            <VideoDepth setError={setError} showNotice={showNotice} />
          )}
          {section === "upscale" && (
            <VideoUpscale setError={setError} showNotice={showNotice} />
          )}
          {section === "h3" && (
            <MiniMaxH3 setError={setError} showNotice={showNotice} />
          )}
          {section === "image" && <ImageCapabilities />}
          {section === "storage" && <OssStorage setError={setError} showNotice={showNotice} />}
          {section === "separation" && (
            <AudioSeparation setError={setError} showNotice={showNotice} />
          )}
          {section === "resources" && (
            <Resources
              health={health}
              ocrHealth={ocrHealth}
              faceHealth={faceHealth}
              refresh={refresh}
              setError={setError}
              showNotice={showNotice}
            />
          )}
          {section === "models" && (
            <Models
              health={health}
              voices={voices}
              providers={providers}
              reload={loadTts}
              setError={setError}
              showNotice={showNotice}
            />
          )}
          {section === "logs" && <Logs jobs={jobs} setError={setError} />}
          {section === "audit" && <Audit events={audit} reload={loadAudit} />}
          {section === "project" && <ProjectStatus />}
          {section === "bugs" && <Bugs />}
          {section === "roadmap" && <Roadmap />}
          {section === "settings" && <Settings health={health} />}
          {section === "api-keys" && (
            <ApiKeyManagement setError={setError} showNotice={showNotice} />
          )}
          {section === "channels" && <ChannelManagement setError={setError} showNotice={showNotice} />}
          {section === "ai-models" && <AiModelManagement setError={setError} />}
          {BUSINESS_SECTIONS[section] && (
            <RecentTasks
              service={BUSINESS_SECTIONS[section]!}
              setError={setError}
            />
          )}
          {section === "ocr" && (
            <RecentTasks service="subtitle" setError={setError} />
          )}
        </main>
      </div>
    </div>
  );
}

type AiChannel = { id:string; name:string; code:string; deployment_type:"local"|"third_party"; adapter:string; base_url:string; enabled:boolean; priority:number; timeout_seconds:number; health_status:string; credential_configured:boolean; credential_tail?:string|null; last_checked_at?:string|null; last_error?:string|null };
type AiModel = { id:string; name:string; code:string; capability_type:string; input_modalities:string[]; output_modality:string; enabled:boolean; channels:Array<{id:string;channel_code:string;channel_name:string;deployment_type:string;upstream_model:string;enabled:boolean;channel_enabled:boolean;health_status:string;capabilities:Record<string,number>}> };
function uiError(value:unknown,fallback="操作失败"){return value instanceof Error?value.message:fallback}

function ChannelManagement({setError,showNotice}:{setError:(v:string)=>void;showNotice:(v:string)=>void}) {
  const [items,setItems]=useState<AiChannel[]>([]); const [editing,setEditing]=useState<AiChannel|null>(null); const [creating,setCreating]=useState(false); const [busy,setBusy]=useState(false);
  const load=useCallback(async()=>{try{const r=await apiRequest<{items:AiChannel[]}>("control","/internal/admin/ai-capabilities/channels");setItems(r.items)}catch(e){setError(uiError(e))}},[setError]);
  useEffect(()=>{void load()},[load]);
  async function save(event:FormEvent<HTMLFormElement>){event.preventDefault();setBusy(true);const fd=new FormData(event.currentTarget);const credential=String(fd.get("credential")||"");const body={name:String(fd.get("name")),...(editing?{}:{code:String(fd.get("code"))}),deployment_type:String(fd.get("deployment_type")),adapter:String(fd.get("adapter")),base_url:String(fd.get("base_url")),...(credential?{credential}:{}),auth_type:String(fd.get("auth_type")),priority:Number(fd.get("priority")),timeout_seconds:Number(fd.get("timeout_seconds"))};try{await apiRequest("control",editing?`/internal/admin/ai-capabilities/channels/${editing.id}`:"/internal/admin/ai-capabilities/channels",{method:editing?"PATCH":"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body)});showNotice("渠道配置已保存");setEditing(null);setCreating(false);await load()}catch(e){setError(uiError(e))}finally{setBusy(false)}}
  async function action(item:AiChannel,action:"test"|"toggle"|"delete"){setBusy(true);try{if(action==="test")await apiRequest("control",`/internal/admin/ai-capabilities/channels/${item.id}/test`,{method:"POST"});else if(action==="toggle")await apiRequest("control",`/internal/admin/ai-capabilities/channels/${item.id}`,{method:"PATCH",headers:{"content-type":"application/json"},body:JSON.stringify({enabled:!item.enabled})});else await apiRequest("control",`/internal/admin/ai-capabilities/channels/${item.id}`,{method:"DELETE"});showNotice(action==="test"?"连接检测完成":"渠道状态已更新");await load()}catch(e){setError(uiError(e))}finally{setBusy(false)}}
  return <><div className="page-title-row"><div><span className="eyebrow">AI CAPABILITIES</span><h2>渠道管理</h2><p>统一管理本地部署和第三方接口。密钥加密保存且仅显示尾部。</p></div><button className="primary-button" onClick={()=>setCreating(true)}>新增渠道</button></div><Panel title="模型与生成渠道" eyebrow={`${items.length} CHANNELS`}><div className="table-shell"><table><thead><tr><th>渠道</th><th>类型</th><th>连接配置</th><th>状态</th><th>优先级</th><th>操作</th></tr></thead><tbody>{items.map(item=><tr key={item.id}><td><strong>{item.name}</strong><small>{item.code}</small></td><td>{item.deployment_type==="local"?"本地部署":"第三方接口"}</td><td><span>{item.base_url||"未配置地址"}</span><small>{item.credential_configured?`密钥 ····${item.credential_tail||""}`:"未配置密钥"}</small></td><td><StatusBadge status={item.enabled?item.health_status:"disabled"}/>{item.last_error&&<small>{item.last_error}</small>}</td><td>{item.priority}</td><td><div className="button-row"><button disabled={busy} onClick={()=>void action(item,"test")}>测试连接</button><button onClick={()=>setEditing(item)}>编辑</button><button disabled={busy} onClick={()=>void action(item,"toggle")}>{item.enabled?"停用":"启用"}</button>{!["local","jmapi","libtv","grsai"].includes(item.code)&&<button className="danger-button" onClick={()=>void action(item,"delete")}>删除</button>}</div></td></tr>)}</tbody></table></div></Panel>{(creating||editing)&&<Modal title={editing?`编辑 ${editing.name}`:"新增渠道"} onClose={()=>{setCreating(false);setEditing(null)}}><form className="form-grid" onSubmit={save}><label>名称<input name="name" required defaultValue={editing?.name}/></label>{!editing&&<label>唯一编码<input name="code" required pattern="[a-z][a-z0-9_-]+"/></label>}<label>部署类型<Select name="deployment_type" defaultValue={editing?.deployment_type||"third_party"}><option value="third_party">第三方接口</option><option value="local">本地部署</option></Select></label><label>协议适配器<Select name="adapter" defaultValue={editing?.adapter||"jmapi"}><option value="jmapi">jmapi</option><option value="libtv">libtv</option><option value="grsai">GRSAI</option><option value="local_h3">本地 MiniMax H3</option></Select></label><label className="full-span">Base URL<input name="base_url" type="url" defaultValue={editing?.base_url}/></label><label>鉴权方式<Select name="auth_type" defaultValue="none"><option value="none">无</option><option value="bearer">Bearer</option><option value="x-api-key">X-API-Key</option></Select></label><label>密钥<input name="credential" type="password" placeholder={editing?.credential_configured?"留空则保持原密钥":"可选"}/></label><label>优先级<input name="priority" type="number" min="1" max="1000" defaultValue={editing?.priority||100}/></label><label>超时（秒）<input name="timeout_seconds" type="number" min="5" max="14400" defaultValue={editing?.timeout_seconds||1800}/></label><div className="button-row full-span"><button type="button" onClick={()=>{setCreating(false);setEditing(null)}}>取消</button><button className="primary-button" disabled={busy}>{busy?"保存中…":"保存"}</button></div></form></Modal>}</>
}

function AiModelManagement({setError}:{setError:(v:string)=>void}) {
  const [items,setItems]=useState<AiModel[]>([]);
  useEffect(()=>{apiRequest<{items:AiModel[]}>("control","/internal/admin/ai-capabilities/models").then(r=>setItems(r.items)).catch(e=>setError(uiError(e)))},[setError]);
  return <><div className="page-title-row"><div><span className="eyebrow">AI CAPABILITIES</span><h2>模型管理</h2><p>一个逻辑模型可以绑定多个本地或第三方渠道。</p></div></div><div className="card-grid">{items.map(model=><Panel key={model.id} title={model.name} eyebrow={model.code}><div className="summary-grid"><div><small>能力类型</small><strong>{model.capability_type==="video_generation"?"视频生成":model.capability_type}</strong></div><div><small>输入 / 输出</small><strong>{model.input_modalities.join("、")} → {model.output_modality}</strong></div></div><div className="provider-list">{model.channels.map(channel=><article key={channel.id}><div><strong>{channel.channel_name}</strong><small>{channel.deployment_type==="local"?"本地部署":"第三方接口"} · {channel.upstream_model}</small></div><StatusBadge status={channel.channel_enabled&&channel.enabled?channel.health_status:"disabled"}/><pre>{`图片 ${channel.capabilities.images||0} · 视频 ${channel.capabilities.videos||0} · 音频 ${channel.capabilities.audios||0}`}</pre></article>)}</div></Panel>)}</div></>
}

const SERVICE_NAMES: Record<string, string> = {
  asr: "语音识别",
  tts: "语音合成",
  ocr: "OCR",
  subtitle: "字幕检测",
  lipsync: "唇形驱动",
  face: "人脸处理",
  scene: "视频切片",
  depth: "视频深度",
  upscale: "视频超分",
  separation: "音频分离",
  h3: "MiniMax H3",
  watermark: "水印处理",
};
const OPERATION_NAMES: Record<string, string> = {
  transcribe: "识别",
  transcribe_upload: "上传识别",
  synthesize: "合成",
  synthesize_stream: "流式合成",
  synthesize_async: "异步合成",
  create: "创建任务",
  process: "处理",
  detect: "检测",
  infer: "推理",
  separate: "分离",
  generate: "生成",
};
function serviceName(value: string) {
  return SERVICE_NAMES[value] || value.toUpperCase();
}
function operationName(value: string) {
  return OPERATION_NAMES[value] || value.replaceAll("_", " ");
}
function taskError(task: UnifiedTask) {
  const result = task.result_summary as
    | Record<string, unknown>
    | null
    | undefined;
  return String(result?.error || result?.detail || "任务执行失败").slice(
    0,
    120,
  );
}

type ChartPoint = { label: string; [key: string]: string | number | null };
function MetricLineChart({
  data,
  series,
  valueLabel = (value) => String(Math.round(value)),
}: {
  data: ChartPoint[];
  series: Array<{ key: string; label: string; className: string }>;
  valueLabel?: (value: number) => string;
}) {
  const available = data
    .flatMap((item) => series.map((line) => item[line.key]))
    .filter(
      (value): value is number =>
        typeof value === "number" && Number.isFinite(value),
    );
  if (!data.length || !available.length)
    return <div className="chart-empty">暂无真实样本</div>;
  const width = 620,
    height = 190,
    pad = 22,
    max = Math.max(1, ...available);
  const x = (index: number) =>
    pad + index * ((width - pad * 2) / Math.max(1, data.length - 1));
  const y = (value: number) =>
    height - pad - (value / max) * (height - pad * 2);
  const segments = (key: string) => {
    const groups: string[] = [];
    let current: string[] = [];
    data.forEach((item, index) => {
      const value = item[key];
      if (typeof value === "number" && Number.isFinite(value))
        current.push(`${x(index)},${y(value)}`);
      else if (current.length) {
        groups.push(current.join(" "));
        current = [];
      }
    });
    if (current.length) groups.push(current.join(" "));
    return groups;
  };
  return (
    <div className="mini-chart">
      <div className="chart-legend">
        {series.map((line) => (
          <span key={line.key}>
            <i className={line.className} />
            {line.label}
          </span>
        ))}
      </div>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={series.map((item) => item.label).join("、")}
      >
        <path
          className="chart-grid-line"
          d={`M${pad} ${height - pad}H${width - pad} M${pad} ${height / 2}H${width - pad}`}
        />
        {series.flatMap((line) =>
          segments(line.key).map((points, index) => (
            <polyline
              key={`${line.key}-${index}`}
              className={`chart-line ${line.className}`}
              points={points}
            />
          )),
        )}
        {data.flatMap((item, index) =>
          series.map((line) => {
            const value = item[line.key];
            return typeof value === "number" ? (
              <circle
                key={`${item.label}-${line.key}`}
                className={`chart-point ${line.className}`}
                cx={x(index)}
                cy={y(value)}
                r="3"
              >
                <title>
                  {item.label} · {line.label} {valueLabel(value)}
                </title>
              </circle>
            ) : null;
          }),
        )}
      </svg>
      <div className="chart-axis">
        <span>{data[0].label}</span>
        <span>{data[data.length - 1].label}</span>
      </div>
    </div>
  );
}

function metricSeconds(value?: number | null) {
  return value === null || value === undefined
    ? "—"
    : `${(value / 1000).toFixed(value >= 10000 ? 1 : 2)}s`;
}
function h3Seconds(value?: number | null) {
  return value === null || value === undefined
    ? "—"
    : `${value.toFixed(value >= 100 ? 1 : 2)}s`;
}
const H3_STAGE_LABELS: Record<string, string> = {
  asset_download: "素材下载",
  queue_wait: "等待Worker",
  worker_upload: "上传Worker",
  generation: "模型生成",
  delivery_transcode: "交付转码",
  merge: "视频拼接",
  oss_upload: "OSS上传",
  retry_wait: "重试等待",
};
function calendarSeries(
  from: string,
  to: string,
  points: AnalyticsTimeseries["points"],
): ChartPoint[] {
  const source = new Map(points.map((item) => [item.day, item]));
  const result: ChartPoint[] = [];
  const cursor = new Date(`${from}T00:00:00Z`),
    end = new Date(`${to}T00:00:00Z`);
  while (cursor <= end) {
    const day = cursor.toISOString().slice(0, 10),
      item = source.get(day);
    result.push({
      label: day.slice(5),
      calls: item?.calls ?? null,
      call_successes: item?.call_successes ?? null,
      task_successes: item?.task_successes ?? null,
      task_failures: item?.task_failures ?? null,
      api_p95: item?.api_latency_ms.p95 ?? null,
      task_p95: item?.task_latency_ms.p95 ?? null,
    });
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return result;
}

function Analytics({
  health,
  jobs,
  ocrHealth,
  faceHealth,
  setError,
}: {
  health: Health | null;
  jobs: LipSyncJob[];
  ocrHealth: Record<string, unknown> | null;
  faceHealth: Record<string, unknown> | null;
  setError: (value: string) => void;
}) {
  const today = UI_TODAY;
  const [range, setRange] = useState<"today" | "7" | "30" | "custom">("7");
  const [from, setFrom] = useState(UI_SEVEN_DAYS_AGO);
  const [to, setTo] = useState(today);
  const [service, setService] = useState("");
  const [operation, setOperation] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [data, setData] = useState<AnalyticsSummary | null>(null);
  const [timeseries, setTimeseries] = useState<AnalyticsTimeseries | null>(
    null,
  );
  const [stages, setStages] = useState<AnalyticsStages | null>(null);
  const [h3, setH3] = useState<H3DurationAnalytics | null>(null);
  const [machineType, setMachineType] = useState("");
  const [resolution, setResolution] = useState("");
  const [inputMode, setInputMode] = useState("");
  const [segmentMode, setSegmentMode] = useState("");
  const [loading, setLoading] = useState(false);
  const [failedTasks, setFailedTasks] = useState<UnifiedTask[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const query = new URLSearchParams({ from, to });
      if (service) query.set("service", service);
      if (operation) query.set("operation", operation);
      if (statusFilter) query.set("status", statusFilter);
      const h3Query = new URLSearchParams({
        from,
        to,
        duration_min: "4",
        duration_max: "15",
      });
      if (machineType) h3Query.set("machine_type", machineType);
      if (resolution) h3Query.set("resolution", resolution);
      if (inputMode) h3Query.set("input_mode", inputMode);
      if (segmentMode) h3Query.set("segment_mode", segmentMode);
      const [summaryResult, trendResult, stageResult, h3Result, failures] =
        await Promise.all([
          apiRequest<AnalyticsSummary>(
            "control",
            `/internal/admin/observability/analytics/summary?${query}`,
          ),
          apiRequest<AnalyticsTimeseries>(
            "control",
            `/internal/admin/observability/analytics/timeseries?${query}`,
          ),
          apiRequest<AnalyticsStages>(
            "control",
            `/internal/admin/observability/analytics/stages?${query}`,
          ),
          service === "" || service === "h3"
            ? apiRequest<H3DurationAnalytics>(
                "control",
                `/internal/admin/observability/analytics/h3-duration?${h3Query}`,
              )
            : Promise.resolve(null),
          apiRequest<{ items: UnifiedTask[] }>(
            "control",
            `/internal/admin/observability/tasks?${new URLSearchParams({ from, to, status: "failed", page_size: "8", ...(service ? { service } : {}) })}`,
          ),
        ]);
      setData(summaryResult);
      setTimeseries(trendResult);
      setStages(stageResult);
      setH3(h3Result);
      setFailedTasks(failures.items);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "统计数据加载失败");
    } finally {
      setLoading(false);
    }
  }, [
    from,
    to,
    service,
    operation,
    statusFilter,
    machineType,
    resolution,
    inputMode,
    segmentMode,
    setError,
  ]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  function preset(days: 7 | 30) {
    setRange(String(days) as "7" | "30");
    setFrom(dateInput(new Date(Date.now() - (days - 1) * 86_400_000)));
    setTo(today);
  }

  function todayPreset() {
    setRange("today");
    setFrom(today);
    setTo(today);
  }

  const trendPoints = calendarSeries(from, to, timeseries?.points || []);
  const activeTasks = Object.entries(data?.tasks.statuses || {})
    .filter(([status]) =>
      ["queued", "running", "retrying", "pending"].includes(status),
    )
    .reduce((sum, [, count]) => sum + count, 0);
  const operationOptions = Array.from(
    new Set((data?.endpoints || []).map((item) => item.operation)),
  ).sort();
  const h3DurationPoints: ChartPoint[] = (h3?.durations || []).map((item) => ({
    label: `${item.duration_seconds}s`,
    mean: item.end_to_end.mean,
    p50: item.end_to_end.p50,
    p95: item.end_to_end.p95,
  }));
  const h3StageNames = Object.keys(H3_STAGE_LABELS);

  return (
    <>
      <PageHeading
        eyebrow="OPERATIONS OVERVIEW"
        title="数据分析"
        description="调用、任务、稳定性与成本的统一运营视图。"
        actions={
          <button className="secondary-button" onClick={() => void load()}>
            {loading ? "刷新中…" : "刷新数据"}
          </button>
        }
      />
      <div className="analytics-toolbar">
        <div className="segmented">
          <button
            className={range === "today" ? "is-active" : ""}
            onClick={todayPreset}
          >
            今天
          </button>
          <button
            className={range === "7" ? "is-active" : ""}
            onClick={() => preset(7)}
          >
            近7天
          </button>
          <button
            className={range === "30" ? "is-active" : ""}
            onClick={() => preset(30)}
          >
            近30天
          </button>
          <button
            className={range === "custom" ? "is-active" : ""}
            onClick={() => setRange("custom")}
          >
            自定义
          </button>
        </div>
        <label>
          开始
          <input
            type="date"
            value={from}
            max={to}
            onChange={(event) => {
              setFrom(event.target.value);
              setRange("custom");
            }}
          />
        </label>
        <label>
          结束
          <input
            type="date"
            value={to}
            min={from}
            max={today}
            onChange={(event) => {
              setTo(event.target.value);
              setRange("custom");
            }}
          />
        </label>
        <label>
          服务
          <Select
            value={service}
            onChange={(event) => {
              setService(event.target.value);
              setOperation("");
            }}
          >
            <option value="">全部服务</option>
            {[
              "asr",
              "tts",
              "ocr",
              "subtitle",
              "lipsync",
              "face",
              "scene",
              "watermark",
              "depth",
              "upscale",
              "separation",
              "h3",
            ].map((item) => (
              <option key={item} value={item}>
                {serviceName(item)}
              </option>
            ))}
          </Select>
        </label>
        <label>
          接口
          <Select
            value={operation}
            onChange={(event) => setOperation(event.target.value)}
          >
            <option value="">全部接口</option>
            {operationOptions.map((item) => (
              <option key={item} value={item}>
                {operationName(item)}
              </option>
            ))}
          </Select>
        </label>
        <label>
          任务状态
          <Select
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}
          >
            <option value="">全部状态</option>
            {["queued", "running", "succeeded", "failed", "cancelled"].map(
              (item) => (
                <option key={item}>{item}</option>
              ),
            )}
          </Select>
        </label>
      </div>
      <div className="metric-grid metrics-six">
        <article className="metric-card accent">
          <span>API请求</span>
          <strong>{data?.api.calls ?? "—"}</strong>
          <small>采集起始 {formatTime(data?.collection_started_at)}</small>
        </article>
        <article className="metric-card">
          <span>HTTP成功率</span>
          <strong>{percent(data?.api.success_rate)}</strong>
          <small>{data?.api.successes ?? 0} 次成功，仅表示接口响应</small>
        </article>
        <article className="metric-card">
          <span>最终任务成功率</span>
          <strong>{percent(data?.tasks.success_rate)}</strong>
          <small>
            {data?.tasks.succeeded ?? 0} 成功 / {data?.tasks.failed ?? 0} 失败
          </small>
        </article>
        <article className="metric-card">
          <span>API响应 P95</span>
          <strong>{metricSeconds(data?.api.latency_ms.p95)}</strong>
          <small>
            平均 {metricSeconds(data?.api.latency_ms.mean)} · n=
            {data?.api.latency_ms.n ?? 0}
          </small>
        </article>
        <article className="metric-card">
          <span>任务端到端 P95</span>
          <strong>{metricSeconds(data?.tasks.latency_ms.p95)}</strong>
          <small>
            P50 {metricSeconds(data?.tasks.latency_ms.p50)} · 平均{" "}
            {metricSeconds(data?.tasks.latency_ms.mean)}
          </small>
        </article>
        <article className="metric-card">
          <span>排队与运行</span>
          <strong>{activeTasks}</strong>
          <small>所有异步服务</small>
        </article>
        <article className="metric-card">
          <span>估算费用</span>
          <strong>{money(data?.billing.amount)}</strong>
          <small>
            损耗 {money(data?.billing.waste_amount)} · 未定价{" "}
            {data?.billing.unpriced_tasks || 0}
          </small>
        </article>
      </div>
      <div className="analytics-chart-grid">
        <Panel title="接口调用趋势" eyebrow="HTTP REQUESTS">
          <MetricLineChart
            data={trendPoints}
            series={[
              { key: "calls", label: "调用", className: "primary" },
              {
                key: "call_successes",
                label: "HTTP成功",
                className: "secondary",
              },
            ]}
          />
        </Panel>
        <Panel title="任务最终结果" eyebrow="FINAL OUTCOMES">
          <MetricLineChart
            data={trendPoints}
            series={[
              { key: "task_successes", label: "成功", className: "primary" },
              { key: "task_failures", label: "失败", className: "danger" },
            ]}
          />
        </Panel>
        <Panel title="真实延迟 P95" eyebrow="LATENCY">
          <MetricLineChart
            data={trendPoints}
            series={[
              { key: "api_p95", label: "API响应", className: "secondary" },
              { key: "task_p95", label: "任务端到端", className: "primary" },
            ]}
            valueLabel={(value) => metricSeconds(value)}
          />
        </Panel>
      </div>
      <Panel title="接口明细" eyebrow="REAL METRICS BY ENDPOINT">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>服务 / 接口</th>
                <th>API请求</th>
                <th>HTTP成功率</th>
                <th>最终任务</th>
                <th>任务成功率</th>
                <th>API P95</th>
                <th>任务平均 / P50 / P95</th>
                <th>最近错误</th>
              </tr>
            </thead>
            <tbody>
              {!data?.endpoints.length && (
                <tr>
                  <td colSpan={8} className="empty-cell">
                    所选范围暂无真实数据
                  </td>
                </tr>
              )}
              {data?.endpoints.map((item) => (
                <tr key={`${item.service}-${item.operation}`}>
                  <td>
                    <strong>{serviceName(item.service)}</strong>
                    <small>{operationName(item.operation)}</small>
                  </td>
                  <td>{item.calls}</td>
                  <td>{percent(item.call_success_rate)}</td>
                  <td>
                    {item.task_successes}/{item.terminal_tasks}
                  </td>
                  <td>{percent(item.task_success_rate)}</td>
                  <td>
                    {metricSeconds(item.api_latency_ms.p95)}
                    <small>n={item.api_latency_ms.n}</small>
                  </td>
                  <td>
                    {metricSeconds(item.task_latency_ms.mean)} /{" "}
                    {metricSeconds(item.task_latency_ms.p50)} /{" "}
                    {metricSeconds(item.task_latency_ms.p95)}
                    <small>n={item.task_latency_ms.n}</small>
                  </td>
                  <td>{item.last_error || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel title="服务阶段覆盖" eyebrow="MEASURED STAGES">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>服务</th>
                <th>阶段</th>
                <th>平均</th>
                <th>P50</th>
                <th>P95</th>
                <th>样本</th>
                <th>来源</th>
              </tr>
            </thead>
            <tbody>
              {!stages?.stages.length && (
                <tr>
                  <td colSpan={7} className="empty-cell">
                    暂无真实阶段样本
                  </td>
                </tr>
              )}
              {stages?.stages.map((item) => (
                <tr key={`${item.service}-${item.stage}`}>
                  <td>{serviceName(item.service)}</td>
                  <td>{item.label}</td>
                  <td>{metricSeconds(item.latency_ms.mean)}</td>
                  <td>{metricSeconds(item.latency_ms.p50)}</td>
                  <td>{metricSeconds(item.latency_ms.p95)}</td>
                  <td>{item.latency_ms.n}</td>
                  <td>{item.source === "measured" ? "精确" : "估算"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="panel-note">{stages?.note}</p>
      </Panel>
      {(service === "" || service === "h3") && (
        <section className="h3-analytics-section">
          <div className="section-heading">
            <div>
              <span>MINIMAX H3 · PRODUCTION DATA</span>
              <h2>4～15秒分阶段性能</h2>
              <p>真实生产任务，不剔除长尾；4090 24G与4090 48G分开统计。</p>
            </div>
          </div>
          <div className="analytics-toolbar compact">
            <label>
              GPU机型
              <Select
                value={machineType}
                onChange={(event) => setMachineType(event.target.value)}
              >
                <option value="">全部机型</option>
                <option value="4090_24g">RTX 4090 · 24G</option>
                <option value="4090_48g">RTX 4090 · 48G</option>
                <option value="5090_32g">RTX 5090 · 32G</option>
              </Select>
            </label>
            <label>
              分辨率
              <Select
                value={resolution}
                onChange={(event) => setResolution(event.target.value)}
              >
                <option value="">全部</option>
                {["480p", "720p", "1080p"].map((item) => (
                  <option key={item}>{item}</option>
                ))}
              </Select>
            </label>
            <label>
              输入模式
              <Select
                value={inputMode}
                onChange={(event) => setInputMode(event.target.value)}
              >
                <option value="">全部</option>
                <option value="text">纯文本</option>
                <option value="image">图片</option>
                <option value="video">视频</option>
                <option value="audio">音频</option>
                <option value="multimodal">多模态</option>
              </Select>
            </label>
            <label>
              分段
              <Select
                value={segmentMode}
                onChange={(event) => setSegmentMode(event.target.value)}
              >
                <option value="">全部</option>
                <option value="single">单段</option>
                <option value="two_part">两段</option>
              </Select>
            </label>
          </div>
          <div className="metric-grid metrics-six">
            <article className="metric-card">
              <span>最终成功率</span>
              <strong>{percent(h3?.success_rate)}</strong>
              <small>
                {h3?.succeeded_jobs ?? 0}/{h3?.terminal_jobs ?? 0} 个终态任务
              </small>
            </article>
            <article className="metric-card">
              <span>提交响应 P95</span>
              <strong>{metricSeconds(h3?.submission_latency_ms.p95)}</strong>
              <small>HTTP受理耗时 · n={h3?.submission_latency_ms.n ?? 0}</small>
            </article>
            <article className="metric-card">
              <span>端到端 P95</span>
              <strong>{h3Seconds(h3?.overall.end_to_end.p95)}</strong>
              <small>
                平均 {h3Seconds(h3?.overall.end_to_end.mean)} · n=
                {h3?.overall.end_to_end.n ?? 0}
              </small>
            </article>
            <article className="metric-card">
              <span>重试率</span>
              <strong>{percent(h3?.retry_rate)}</strong>
              <small>{h3?.retry_jobs ?? 0} 个任务发生重试</small>
            </article>
            <article className="metric-card">
              <span>产出吞吐</span>
              <strong>
                {h3?.generated_seconds_per_processing_hour == null
                  ? "—"
                  : `${h3.generated_seconds_per_processing_hour.toFixed(1)}秒`}
              </strong>
              <small>每处理小时生成视频秒数</small>
            </article>
            <article className="metric-card">
              <span>精确 / 估算样本</span>
              <strong>
                {h3?.coverage.measured ?? 0} / {h3?.coverage.estimated ?? 0}
              </strong>
              <small>新计时 / 历史阶段回算</small>
            </article>
          </div>
          <div className="two-column wide-left">
            <Panel title="时长与端到端耗时" eyebrow="MEAN / P50 / P95">
              <MetricLineChart
                data={h3DurationPoints}
                series={[
                  { key: "mean", label: "平均", className: "secondary" },
                  { key: "p50", label: "P50", className: "primary" },
                  { key: "p95", label: "P95", className: "danger" },
                ]}
                valueLabel={h3Seconds}
              />
            </Panel>
            <Panel title="机型对比" eyebrow="GPU MODELS">
              <div className="service-ranking">
                {Object.entries(h3?.by_machine_type || {}).map(
                  ([machine, value]) => (
                    <article key={machine}>
                      <div>
                        <strong>
                          {machine.replaceAll("_", " · ").toUpperCase()}
                        </strong>
                        <small>
                          平均 {h3Seconds(value.end_to_end.mean)} · P50{" "}
                          {h3Seconds(value.end_to_end.p50)} · P95{" "}
                          {h3Seconds(value.end_to_end.p95)}
                        </small>
                      </div>
                      <span>n={value.end_to_end.n}</span>
                    </article>
                  ),
                )}
                {!Object.keys(h3?.by_machine_type || {}).length && (
                  <p className="empty-note">暂无机型样本</p>
                )}
              </div>
            </Panel>
          </div>
          <Panel title="逐时长阶段耗时（平均值）" eyebrow="STAGE BREAKDOWN">
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>时长</th>
                    <th>样本</th>
                    {h3StageNames.map((stage) => (
                      <th key={stage}>{H3_STAGE_LABELS[stage]}</th>
                    ))}
                    <th>端到端 平均/P50/P95</th>
                  </tr>
                </thead>
                <tbody>
                  {h3?.durations.map((item) => (
                    <tr key={item.duration_seconds}>
                      <td>
                        <strong>{item.duration_seconds}s</strong>
                      </td>
                      <td>{item.end_to_end.n || "—"}</td>
                      {h3StageNames.map((stage) => (
                        <td key={stage}>
                          {h3Seconds(item.stages[stage]?.mean)}
                        </td>
                      ))}
                      <td>
                        {h3Seconds(item.end_to_end.mean)} /{" "}
                        {h3Seconds(item.end_to_end.p50)} /{" "}
                        {h3Seconds(item.end_to_end.p95)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        </section>
      )}
      {(service === "" || service === "h3") && (
        <Panel title="H3逐时长阶段分位数" eyebrow="MEAN / P50 / P95">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>时长</th>
                  <th>阶段</th>
                  <th>平均</th>
                  <th>P50</th>
                  <th>P95</th>
                  <th>样本数</th>
                </tr>
              </thead>
              <tbody>
                {h3?.durations.flatMap((item) =>
                  h3StageNames
                    .filter((stage) => (item.stages[stage]?.n || 0) > 0)
                    .map((stage) => (
                      <tr key={`${item.duration_seconds}-${stage}`}>
                        <td>{item.duration_seconds}s</td>
                        <td>{H3_STAGE_LABELS[stage]}</td>
                        <td>{h3Seconds(item.stages[stage].mean)}</td>
                        <td>{h3Seconds(item.stages[stage].p50)}</td>
                        <td>{h3Seconds(item.stages[stage].p95)}</td>
                        <td>{item.stages[stage].n}</td>
                      </tr>
                    )),
                )}
                {!h3?.durations.some((item) =>
                  h3StageNames.some(
                    (stage) => (item.stages[stage]?.n || 0) > 0,
                  ),
                ) && (
                  <tr>
                    <td colSpan={6} className="empty-cell">
                      所选条件没有可计算的阶段样本
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
      <Panel title="最近失败任务" eyebrow="RECENT FAILURES">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>服务</th>
                <th>任务 ID</th>
                <th>失败阶段</th>
                <th>错误摘要</th>
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              {failedTasks.length === 0 && (
                <tr>
                  <td colSpan={6} className="empty-cell">
                    所选范围没有失败任务
                  </td>
                </tr>
              )}
              {failedTasks.map((task) => (
                <tr key={task.id}>
                  <td>{formatTime(task.updated_at)}</td>
                  <td>
                    <strong>{serviceName(task.service)}</strong>
                  </td>
                  <td className="mono truncate-id">
                    {task.external_task_id || task.id}
                  </td>
                  <td>{task.stage || "—"}</td>
                  <td className="wrap">{taskError(task)}</td>
                  <td>
                    <Link
                      className="table-link"
                      href={`/calls?service=${task.service}&task_id=${task.external_task_id || task.id}`}
                    >
                      查看
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <div className="two-column wide-left">
        <Panel title="实际用量" eyebrow="USAGE">
          <div className="status-summary">
            {data?.usage.map((item) => (
              <div key={item.unit_type}>
                <span>{item.unit_type}</span>
                <strong>{item.quantity.toLocaleString("zh-CN")}</strong>
              </div>
            ))}
            {!data?.usage.length && <p className="empty-note">暂无用量记录</p>}
          </div>
        </Panel>
        <Panel title="任务状态分布" eyebrow="FINAL OUTCOMES">
          <div className="status-summary">
            {Object.entries(data?.tasks.statuses || {}).map(
              ([status, count]) => (
                <div key={status}>
                  <StatusBadge status={status} />
                  <strong>{count}</strong>
                </div>
              ),
            )}
            {!Object.keys(data?.tasks.statuses || {}).length && (
              <p className="empty-note">暂无任务记录</p>
            )}
          </div>
        </Panel>
      </div>
      <div className="two-column">
        <Panel title="错误排行" eyebrow="TOP ERRORS">
          <div className="error-ranking">
            {(data?.errors || []).map((item) => (
              <div key={item.error_code}>
                <code>{item.error_code}</code>
                <strong>{item.count}</strong>
              </div>
            ))}
            {!data?.errors.length && (
              <p className="empty-note">所选日期没有失败请求。</p>
            )}
          </div>
        </Panel>
        <Panel title="运行摘要" eyebrow="LIVE STATUS">
          <div className="status-summary">
            <div>
              <span>排队与运行</span>
              <strong>{activeTasks}</strong>
            </div>
            <div>
              <span>采集起始</span>
              <strong>{formatTime(data?.collection_started_at)}</strong>
            </div>
          </div>
        </Panel>
      </div>
      <LiveServices
        health={health}
        jobs={jobs}
        ocrHealth={ocrHealth}
        faceHealth={faceHealth}
      />
    </>
  );
}

function CallsAndTasks({ setError }: { setError: (value: string) => void }) {
  const [tab, setTab] = useState<"calls" | "tasks">("calls");
  const [calls, setCalls] = useState<ApiCallRecord[]>([]);
  const [tasks, setTasks] = useState<UnifiedTask[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [service, setService] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [keyword, setKeyword] = useState("");
  const [from, setFrom] = useState(UI_THIRTY_DAYS_AGO);
  const [to, setTo] = useState(UI_TODAY);
  const [detail, setDetail] = useState<{
    kind: "call" | "task";
    value: ApiCallRecord | UnifiedTask;
  } | null>(null);
  const [sensitive, setSensitive] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const query = new URLSearchParams(window.location.search);
      const requestedService = query.get("service");
      const requestedTask = query.get("task_id");
      if (requestedService) setService(requestedService);
      if (requestedTask) {
        setTab("tasks");
        setKeyword(requestedTask);
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const query = new URLSearchParams({
        from,
        to,
        page: String(page),
        page_size: String(pageSize),
      });
      if (service) query.set("service", service);
      if (statusFilter) query.set("status", statusFilter);
      if (keyword) query.set(tab === "calls" ? "trace_id" : "task_id", keyword);
      if (tab === "calls") {
        const result = await apiRequest<{
          items: ApiCallRecord[];
          total: number;
          page: number;
          page_size: number;
        }>("control", `/internal/admin/observability/calls?${query}`);
        setCalls(result.items);
        setTotal(result.total);
      } else {
        const result = await apiRequest<{
          items: UnifiedTask[];
          total: number;
          page: number;
          page_size: number;
        }>("control", `/internal/admin/observability/tasks?${query}`);
        setTasks(result.items);
        setTotal(result.total);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "记录加载失败");
    } finally {
      setLoading(false);
    }
  }, [from, to, service, statusFilter, keyword, tab, page, pageSize, setError]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  async function openCall(call: Pick<ApiCallRecord, "id">) {
    try {
      setSensitive(null);
      setDetail({
        kind: "call",
        value: await apiRequest<ApiCallRecord>(
          "control",
          `/internal/admin/observability/calls/${call.id}`,
        ),
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "调用详情加载失败");
    }
  }
  async function openTask(task: UnifiedTask) {
    try {
      setSensitive(null);
      setDetail({
        kind: "task",
        value: await apiRequest<UnifiedTask>(
          "control",
          `/internal/admin/observability/tasks/${task.id}`,
        ),
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "任务详情加载失败");
    }
  }
  async function reveal(callId: string, retry = true) {
    const response = await fetch(
      `${BASE_PATH}/api/observability/reveal/${callId}`,
      { cache: "no-store" },
    );
    if (response.status === 403 && retry) {
      const password = window.prompt(
        "查看受限请求/响应需要重新输入管理员密码：",
      );
      if (!password) return;
      const verified = await fetch(`${BASE_PATH}/api/auth/reauth`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (!verified.ok) throw new Error("密码验证失败");
      await reveal(callId, false);
      return;
    }
    if (!response.ok)
      throw new Error((await response.json()).detail || "受限详情不可用");
    setSensitive(await response.json());
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <>
      <PageHeading
        eyebrow="TASK & TRACE LOGS"
        title="任务日志"
        description="统一检索API调用和业务任务，通过Trace ID查看请求、响应、执行事件与计费。"
        actions={
          <button className="secondary-button" onClick={() => void load()}>
            {loading ? "加载中…" : "刷新"}
          </button>
        }
      />
      <div className="record-tabs">
        <button
          className={tab === "calls" ? "is-active" : ""}
          onClick={() => {
            setTab("calls");
            setStatusFilter("");
            setPage(1);
          }}
        >
          API调用日志 <span>{tab === "calls" ? total : ""}</span>
        </button>
        <button
          className={tab === "tasks" ? "is-active" : ""}
          onClick={() => {
            setTab("tasks");
            setStatusFilter("");
            setPage(1);
          }}
        >
          业务任务日志 <span>{tab === "tasks" ? total : ""}</span>
        </button>
      </div>
      <Panel title="筛选条件" eyebrow="FILTERS">
        <div className="record-filters">
          <label>
            开始
            <input
              type="date"
              value={from}
              onChange={(event) => {
                setFrom(event.target.value);
                setPage(1);
              }}
            />
          </label>
          <label>
            结束
            <input
              type="date"
              value={to}
              onChange={(event) => {
                setTo(event.target.value);
                setPage(1);
              }}
            />
          </label>
          <label>
            服务
            <Select
              value={service}
              onChange={(event) => {
                setService(event.target.value);
                setPage(1);
              }}
            >
              <option value="">全部</option>
              {[
                "asr",
                "tts",
                "ocr",
                "subtitle",
                "lipsync",
                "face",
                "scene",
                "depth",
                "separation",
                "h3",
              ].map((item) => (
                <option key={item}>{item}</option>
              ))}
            </Select>
          </label>
          <label>
            状态
            <Select
              value={statusFilter}
              onChange={(event) => {
                setStatusFilter(event.target.value);
                setPage(1);
              }}
            >
              <option value="">全部</option>
              {tab === "calls" ? (
                <>
                  <option value="success">成功</option>
                  <option value="failure">失败</option>
                </>
              ) : (
                [
                  "queued",
                  "running",
                  "succeeded",
                  "completed",
                  "failed",
                  "cancelled",
                ].map((item) => <option key={item}>{item}</option>)
              )}
            </Select>
          </label>
          <label className="filter-search">
            {tab === "calls" ? "Trace ID" : "任务 ID"}
            <input
              value={keyword}
              onChange={(event) => {
                setKeyword(event.target.value);
                setPage(1);
              }}
              placeholder="输入部分ID"
            />
          </label>
          <button className="primary-button" onClick={() => void load()}>
            查询
          </button>
        </div>
      </Panel>
      <Panel
        title={tab === "calls" ? "API调用日志" : "业务任务日志"}
        eyebrow={`${total} RECORDS`}
      >
        {tab === "calls" ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>服务/操作</th>
                  <th>路径</th>
                  <th>状态</th>
                  <th>耗时</th>
                  <th>Trace ID</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {calls.length === 0 && (
                  <tr>
                    <td colSpan={7} className="empty-cell">
                      暂无调用日志
                    </td>
                  </tr>
                )}
                {calls.map((call) => (
                  <tr key={call.id}>
                    <td>{formatTime(call.started_at)}</td>
                    <td>
                      <strong>{serviceName(call.service)}</strong>
                      <small>{operationName(call.operation)}</small>
                    </td>
                    <td className="mono wrap">
                      {call.method} {call.path}
                    </td>
                    <td>
                      <StatusBadge status={String(call.status_code)} />
                    </td>
                    <td>{formatDurationMs(call.duration_ms)}</td>
                    <td className="mono">{call.trace_id.slice(0, 12)}</td>
                    <td>
                      <button
                        className="table-link"
                        onClick={() => void openCall(call)}
                      >
                        查看
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <TaskRecordsTable tasks={tasks} onOpen={openTask} />
        )}
        <div className="record-pagination">
          <span>
            第 {page} / {totalPages} 页，共 {total} 条
          </span>
          <label>
            每页
            <Select
              aria-label="每页条数"
              value={pageSize}
              onChange={(event) => {
                setPageSize(Number(event.target.value));
                setPage(1);
              }}
            >
              <option value={20}>20</option>
              <option value={50}>50</option>
              <option value={100}>100</option>
            </Select>
          </label>
          <div>
            <button disabled={loading || page <= 1} onClick={() => setPage(1)}>
              首页
            </button>
            <button
              disabled={loading || page <= 1}
              onClick={() => setPage((value) => Math.max(1, value - 1))}
            >
              上一页
            </button>
            <button
              disabled={loading || page >= totalPages}
              onClick={() =>
                setPage((value) => Math.min(totalPages, value + 1))
              }
            >
              下一页
            </button>
            <button
              disabled={loading || page >= totalPages}
              onClick={() => setPage(totalPages)}
            >
              末页
            </button>
          </div>
        </div>
      </Panel>
      {detail && (
        <div className="drawer-backdrop" onClick={() => setDetail(null)}>
          <aside
            className="detail-drawer"
            onClick={(event) => event.stopPropagation()}
          >
            <header>
              <div>
                <p className="eyebrow">
                  {detail.kind === "call" ? "API CALL" : "BUSINESS TASK"}
                </p>
                <h2>
                  {detail.kind === "call"
                    ? (detail.value as ApiCallRecord).trace_id
                    : (detail.value as UnifiedTask).external_task_id ||
                      detail.value.id}
                </h2>
              </div>
              <button onClick={() => setDetail(null)}>×</button>
            </header>
            {detail.kind === "call" ? (
              <CallDetail
                call={detail.value as ApiCallRecord}
                sensitive={sensitive}
                reveal={(id) =>
                  reveal(id).catch((reason) =>
                    setError(
                      reason instanceof Error ? reason.message : "查看失败",
                    ),
                  )
                }
              />
            ) : (
              <TaskDetail
                task={detail.value as UnifiedTask}
                openCall={(id) => void openCall({ id })}
              />
            )}
          </aside>
        </div>
      )}
    </>
  );
}

function TaskRecordsTable({
  tasks,
  onOpen,
}: {
  tasks: UnifiedTask[];
  onOpen: (task: UnifiedTask) => void;
}) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>创建时间</th>
            <th>服务/操作</th>
            <th>任务ID</th>
            <th>状态/阶段</th>
            <th>用量</th>
            <th>费用</th>
            <th>详情</th>
          </tr>
        </thead>
        <tbody>
          {tasks.length === 0 && (
            <tr>
              <td colSpan={7} className="empty-cell">
                暂无任务日志
              </td>
            </tr>
          )}
          {tasks.map((task) => (
            <tr key={task.id}>
              <td>{formatTime(task.created_at)}</td>
              <td>
                <strong>{serviceName(task.service)}</strong>
                <small>{operationName(task.operation)}</small>
              </td>
              <td className="mono">
                {(task.external_task_id || task.id).slice(0, 16)}
              </td>
              <td>
                <StatusBadge status={task.status} />
                <small>{task.stage}</small>
              </td>
              <td>
                {Number(task.input_quantity).toFixed(3)} {task.unit_type}
              </td>
              <td>
                {task.billing_status === "unpriced"
                  ? "未定价"
                  : money(task.amount)}
                {Number(task.waste_amount || 0) > 0 && (
                  <small>损耗 {money(task.waste_amount)}</small>
                )}
              </td>
              <td>
                <button className="table-link" onClick={() => onOpen(task)}>
                  查看
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CallDetail({
  call,
  sensitive,
  reveal,
}: {
  call: ApiCallRecord;
  sensitive: unknown;
  reveal: (id: string) => void;
}) {
  return (
    <div className="detail-content">
      <dl className="detail-grid">
        <div>
          <dt>服务</dt>
          <dd>
            {call.service}/{call.operation}
          </dd>
        </div>
        <div>
          <dt>HTTP</dt>
          <dd>
            {call.method} · {call.status_code}
          </dd>
        </div>
        <div>
          <dt>耗时</dt>
          <dd>{formatDurationMs(call.duration_ms)}</dd>
        </div>
        <div>
          <dt>时间</dt>
          <dd>{formatTime(call.started_at)}</dd>
        </div>
        <div className="span-2">
          <dt>路径</dt>
          <dd className="mono">{call.path}</dd>
        </div>
        <div className="span-2">
          <dt>Trace ID</dt>
          <dd className="mono">{call.trace_id}</dd>
        </div>
      </dl>
      <section>
        <div className="detail-section-heading">
          <h3>请求参数</h3>
          {Boolean(call.snapshots?.length) && (
            <button
              className="secondary-button"
              onClick={() => reveal(call.id)}
            >
              验证密码查看完整响应正文
            </button>
          )}
        </div>
        <ResultBox value={call.request_summary} empty="没有可展示的请求参数" />
      </section>
      <section>
        <h3>响应摘要</h3>
        <ResultBox value={call.response_summary} empty="没有可展示的响应摘要" />
      </section>
      {sensitive ? (
        <section>
          <h3>受限请求与响应</h3>
          <div className="sensitive-warning">
            内容已解密，本次查看已写入操作审计；Token、Cookie和URL签名仍不会展示。
          </div>
          <ResultBox value={sensitive} empty="受限内容已过期" />
        </section>
      ) : null}
    </div>
  );
}

function TaskDetail({
  task,
  openCall,
}: {
  task: UnifiedTask;
  openCall: (id: string) => void;
}) {
  const linkedCalls = (task.calls || []).slice(0, 10);
  const linkedTotal = task.calls_total ?? linkedCalls.length;
  const originatingCall = [...linkedCalls]
    .reverse()
    .find((call) => call.request_summary);
  return (
    <div className="detail-content">
      <dl className="detail-grid">
        <div>
          <dt>服务</dt>
          <dd>
            {task.service}/{task.operation}
          </dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>
            <StatusBadge status={task.status} />
          </dd>
        </div>
        <div>
          <dt>阶段</dt>
          <dd>{task.stage}</dd>
        </div>
        <div>
          <dt>耗时</dt>
          <dd>{formatDurationMs(task.duration_ms)}</dd>
        </div>
        <div className="span-2">
          <dt>内部任务ID</dt>
          <dd className="mono">{task.id}</dd>
        </div>
        <div className="span-2">
          <dt>业务任务ID</dt>
          <dd className="mono">{task.external_task_id || "同步任务"}</dd>
        </div>
      </dl>
      <section>
        <h3>请求参数</h3>
        <ResultBox
          value={originatingCall?.request_summary}
          empty="暂无请求参数"
        />
      </section>
      <section>
        <h3>响应摘要</h3>
        <ResultBox
          value={originatingCall?.response_summary || task.result_summary}
          empty="暂无响应摘要"
        />
      </section>
      <section>
        <h3>任务事件</h3>
        <div className="event-timeline">
          {(task.events || []).map((event, index) => (
            <article key={index}>
              <i />
              <div>
                <strong>{String(event.stage || event.level)}</strong>
                <p>{String(event.message || "")}</p>
                <small>{formatTime(String(event.timestamp || ""))}</small>
              </div>
            </article>
          ))}
          {!task.events?.length && <p className="empty-note">暂无事件</p>}
        </div>
      </section>
      <section>
        <h3>结果摘要</h3>
        <ResultBox value={task.result_summary} empty="暂无结果摘要" />
      </section>
      <section>
        <h3>计费明细</h3>
        <ResultBox value={task.billing} empty="任务尚未结束或未生成账目" />
      </section>
      <section>
        <h3>关联调用</h3>
        <div className="linked-calls">
          {linkedCalls.map((call, index) => (
            <button key={index} onClick={() => openCall(String(call.id))}>
              <span>
                {String(call.method)} {String(call.path)}
              </span>
              <small>
                {String(call.status_code)} ·{" "}
                {formatDurationMs(Number(call.duration_ms || 0))}
              </small>
            </button>
          ))}
          {linkedCalls.length === 0 && (
            <p className="empty-note">暂无精确关联调用</p>
          )}
        </div>
        {linkedTotal > linkedCalls.length && (
          <p className="linked-calls-note">
            仅显示最近10条精确关联调用，共 {linkedTotal} 条。
          </p>
        )}
      </section>
    </div>
  );
}

function RecentTasks({
  service,
  setError,
}: {
  service: string;
  setError: (value: string) => void;
}) {
  const [tasks, setTasks] = useState<UnifiedTask[]>([]);
  useEffect(() => {
    const timer = window.setTimeout(async () => {
      try {
        const query = new URLSearchParams({ service, page_size: "5" });
        const result = await apiRequest<{ items: UnifiedTask[] }>(
          "control",
          `/internal/admin/observability/tasks?${query}`,
        );
        setTasks(result.items);
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "最近任务加载失败");
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [service, setError]);
  return (
    <Panel title="最近任务记录" eyebrow="UNIFIED TASK HISTORY">
      <TaskRecordsTable
        tasks={tasks}
        onOpen={() => {
          window.location.assign(`${BASE_PATH}/calls`);
        }}
      />
      <div className="panel-foot">
        <Link href="/calls">查看全部调用与任务 →</Link>
      </div>
    </Panel>
  );
}

function BillingCenter({
  setError,
  showNotice,
}: {
  setError: (value: string) => void;
  showNotice: (value: string) => void;
}) {
  const [rules, setRules] = useState<PricingRule[]>([]);
  const [summary, setSummary] = useState<ObservabilityOverview | null>(null);
  const from = UI_THIRTY_DAYS_AGO;
  const to = UI_TODAY;
  const load = useCallback(async () => {
    try {
      const [ruleResult, overview] = await Promise.all([
        apiRequest<{ items: PricingRule[] }>(
          "control",
          "/internal/admin/observability/pricing-rules",
        ),
        apiRequest<ObservabilityOverview>(
          "control",
          `/internal/admin/observability/overview?from=${from}&to=${to}`,
        ),
      ]);
      setRules(ruleResult.items);
      setSummary(overview);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "计费数据加载失败");
    }
  }, [from, to, setError]);
  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      await apiRequest(
        "control",
        "/internal/admin/observability/pricing-rules",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            service: data.get("service"),
            operation: data.get("operation") || "*",
            unit_type: data.get("unit_type"),
            fixed_fee: data.get("fixed_fee") || "0",
            unit_price: data.get("unit_price") || "0",
            effective_from: new Date(
              String(data.get("effective_from")),
            ).toISOString(),
          }),
        },
      );
      showNotice("新价格版本已生效，历史账目不会重算");
      event.currentTarget.reset();
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "价格规则保存失败");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="INTERNAL COST LEDGER"
        title="计费中心"
        description="按价格版本核算内部成本。历史账目保留当时价格快照，失败资源消耗单独列为损耗。"
        actions={
          <a
            className="secondary-button"
            href={`${BASE_PATH}/api/proxy/control/internal/admin/observability/export.csv?from=${from}&to=${to}`}
          >
            导出30天CSV
          </a>
        }
      />
      <div className="metric-grid">
        <article className="metric-card accent">
          <span>30天估算费用</span>
          <strong>{money(summary?.amount)}</strong>
          <small>人民币 · 最终成功任务</small>
        </article>
        <article className="metric-card">
          <span>失败损耗</span>
          <strong>{money(summary?.waste_amount)}</strong>
          <small>已产生资源消耗的失败任务</small>
        </article>
        <article className="metric-card">
          <span>未定价任务</span>
          <strong>{summary?.unpriced_tasks || 0}</strong>
          <small>仅统计用量，不产生金额</small>
        </article>
        <article className="metric-card">
          <span>价格版本</span>
          <strong>{rules.length}</strong>
          <small>修改只影响生效时间后的任务</small>
        </article>
      </div>
      <div className="two-column wide-left">
        <Panel title="价格规则" eyebrow="VERSIONED PRICING">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>服务/操作</th>
                  <th>单位</th>
                  <th>固定费</th>
                  <th>单价</th>
                  <th>生效时间</th>
                </tr>
              </thead>
              <tbody>
                {rules.length === 0 && (
                  <tr>
                    <td colSpan={5} className="empty-cell">
                      尚未配置价格，当前所有任务均按0元统计。
                    </td>
                  </tr>
                )}
                {rules.map((rule) => (
                  <tr key={rule.id}>
                    <td>
                      <strong>{rule.service.toUpperCase()}</strong>
                      <small>{rule.operation}</small>
                    </td>
                    <td>{rule.unit_type}</td>
                    <td>{money(rule.fixed_fee)}</td>
                    <td>{money(rule.unit_price)}</td>
                    <td>{formatTime(rule.effective_from)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
        <Panel title="新增价格版本" eyebrow="NEW RULE">
          <form className="stack-form" onSubmit={submit}>
            <label>
              服务
              <Select name="service" required>
                {[
                  "asr",
                  "tts",
                  "ocr",
                  "subtitle",
                  "lipsync",
                  "face",
                  "scene",
                  "depth",
                  "separation",
                  "h3",
                ].map((item) => (
                  <option key={item}>{item}</option>
                ))}
              </Select>
            </label>
            <label>
              操作
              <input
                name="operation"
                defaultValue="*"
                placeholder="* 表示该服务全部操作"
              />
            </label>
            <label>
              计量单位
              <Select name="unit_type" required>
                <option value="task">每任务</option>
                <option value="audio_minute">音频分钟</option>
                <option value="1000_chars">千字符</option>
                <option value="image">图片/页</option>
                <option value="video_minute">视频分钟</option>
                <option value="output_second">输出音频秒</option>
              </Select>
            </label>
            <div className="field-row">
              <label>
                固定费
                <input
                  name="fixed_fee"
                  type="number"
                  min="0"
                  step="0.000001"
                  defaultValue="0"
                />
              </label>
              <label>
                单位价格
                <input
                  name="unit_price"
                  type="number"
                  min="0"
                  step="0.000001"
                  defaultValue="0"
                />
              </label>
            </div>
            <label>
              生效时间
              <input
                name="effective_from"
                type="datetime-local"
                defaultValue={`${to}T00:00`}
                required
              />
            </label>
            <button className="primary-button">创建价格版本</button>
          </form>
        </Panel>
      </div>
    </>
  );
}

function LiveServices({
  health,
  jobs,
  ocrHealth,
  faceHealth,
}: {
  health: Health | null;
  jobs: LipSyncJob[];
  ocrHealth: Record<string, unknown> | null;
  faceHealth: Record<string, unknown> | null;
}) {
  const services = [
    ...Object.entries(health?.upstreams || {}).map(([name, item]) => ({
      name: name.toUpperCase(),
      status: item.status,
      detail: item.url || "Control plane upstream",
    })),
    {
      name: "OCR",
      status: String(ocrHealth?.status || "unknown"),
      detail: `${ocrHealth?.healthy_workers || 0}/${ocrHealth?.worker_count || 0} workers`,
    },
    {
      name: "FACE",
      status: String(faceHealth?.status || "unknown"),
      detail: `${faceHealth?.worker_count || 0} workers`,
    },
  ];
  return (
    <>
      <div className="dashboard-grid">
        <Panel title="服务矩阵" eyebrow="LIVE SERVICES" className="span-2">
          <div className="service-matrix">
            {services.map((service) => (
              <article key={service.name}>
                <div>
                  <i
                    className={classNames(
                      "signal",
                      service.status === "ok" && "signal-live",
                    )}
                  />
                  <strong>{service.name}</strong>
                </div>
                <StatusBadge status={service.status} />
                <p>{service.detail}</p>
              </article>
            ))}
          </div>
        </Panel>
        <Panel title="性能基线" eyebrow="BENCHMARK">
          <div className="benchmark-list">
            <div>
              <span>3 秒真人样片</span>
              <strong>47.4s</strong>
              <small>MuseTalk 23.1s · GFPGAN 24.2s</small>
            </div>
            <div>
              <span>20 秒真人样片</span>
              <strong>121.9s</strong>
              <small>MuseTalk 63.7s · GFPGAN 58.2s</small>
            </div>
            <div className="benchmark-gain">
              <span>长视频效率</span>
              <strong>2.6×</strong>
              <small>固定启动成本得到摊薄</small>
            </div>
          </div>
        </Panel>
      </div>
      <Panel title="最近唇形任务" eyebrow="RECENT JOBS">
        <JobTable jobs={jobs.slice(0, 6)} />
      </Panel>
    </>
  );
}

function JobTable({
  jobs,
  onCancel,
  onDownload,
}: {
  jobs: LipSyncJob[];
  onCancel?: (job: LipSyncJob) => void;
  onDownload?: (job: LipSyncJob) => void;
}) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>任务</th>
            <th>状态</th>
            <th>阶段</th>
            <th>GFPGAN</th>
            <th>耗时</th>
            <th>创建时间</th>
            {(onCancel || onDownload) && <th>操作</th>}
          </tr>
        </thead>
        <tbody>
          {jobs.length === 0 && (
            <tr>
              <td colSpan={7} className="empty-cell">
                暂无任务
              </td>
            </tr>
          )}
          {jobs.map((job) => (
            <tr key={job.job_id}>
              <td>
                <strong className="mono">{job.job_id.slice(0, 8)}</strong>
                <small>{job.video_filename || "video"}</small>
              </td>
              <td>
                <StatusBadge status={job.state} />
              </td>
              <td>{job.stage}</td>
              <td>{job.face_restore ? "开启" : "关闭"}</td>
              <td>{formatDuration(job.elapsed_seconds)}</td>
              <td>{formatTime(job.created_at)}</td>
              {(onCancel || onDownload) && (
                <td>
                  <div className="row-actions">
                    {job.state === "completed" && onDownload && (
                      <button onClick={() => onDownload(job)}>下载</button>
                    )}
                    {["queued", "running"].includes(job.state) && onCancel && (
                      <button
                        className="danger-link"
                        onClick={() => onCancel(job)}
                      >
                        取消
                      </button>
                    )}
                  </div>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LipSync({
  jobs,
  refresh,
  showNotice,
  setError,
}: {
  jobs: LipSyncJob[];
  refresh: () => Promise<void>;
  showNotice: (message: string) => void;
  setError: (value: string) => void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [filter, setFilter] = useState("all");
  const visible =
    filter === "all" ? jobs : jobs.filter((job) => job.state === filter);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const formElement = event.currentTarget;
      const form = new FormData(formElement);
      await apiRequest("control", "/v1/lipsync/jobs/upload", {
        method: "POST",
        body: form,
      });
      formElement.reset();
      showNotice("任务已持久化并自动进入队列");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "提交失败");
    } finally {
      setSubmitting(false);
    }
  }
  async function cancel(job: LipSyncJob) {
    if (!window.confirm(`确认取消任务 ${job.job_id.slice(0, 8)}？`)) return;
    try {
      await apiRequest("control", `/v1/lipsync/jobs/${job.job_id}/cancel`, {
        method: "POST",
      });
      showNotice("取消请求已发送");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消失败");
    }
  }
  async function download(job: LipSyncJob) {
    try {
      const response = await fetch(
        `${BASE_PATH}/api/proxy/control/v1/lipsync/jobs/${job.job_id}/video`,
      );
      if (!response.ok) throw new Error("视频下载失败");
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${job.job_id}.mp4`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "下载失败");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="MUSETALK + GFPGAN"
        title="唇形驱动"
        description="上传视频和音频后自动持久化、排队和执行；服务重启后任务自动恢复。"
      />
      <div className="two-column">
        <Panel title="创建任务" eyebrow="NEW JOB">
          <form className="stack-form" onSubmit={submit}>
            <label>
              人脸视频
              <input
                name="video"
                type="file"
                accept="video/mp4,video/quicktime,video/webm,.mkv"
                required
              />
            </label>
            <label>
              驱动音频
              <input
                name="audio"
                type="file"
                accept="audio/*,.wav,.mp3,.m4a,.aac,.flac,.ogg"
                required
              />
            </label>
            <label className="toggle-row">
              <span>
                <strong>GFPGAN 人脸修复</strong>
                <small>真人通常更清晰，动漫可能身份漂移</small>
              </span>
              <input name="face_restore" type="checkbox" value="true" />
            </label>
            <button className="primary-button" disabled={submitting}>
              {submitting ? "正在上传…" : "提交并自动执行"}
            </button>
          </form>
        </Panel>
        <Panel title="生产约束" eyebrow="GUARDRAILS">
          <ul className="check-list">
            <li>GPU0 单任务并发，避免显存争抢</li>
            <li>单文件最大 512 MiB</li>
            <li>状态原子落盘，重启自动恢复</li>
            <li>GFPGAN 默认关闭，按任务显式开启</li>
          </ul>
          <div className="note-card">
            <b>建议</b>
            <p>清晰真人可开启修复；动漫、插画和已锐化素材建议关闭。</p>
          </div>
        </Panel>
      </div>
      <Panel title="任务记录" eyebrow="PERSISTED JOBS">
        <div className="filter-bar">
          {["all", "queued", "running", "completed", "failed", "cancelled"].map(
            (item) => (
              <button
                className={filter === item ? "is-active" : ""}
                onClick={() => setFilter(item)}
                key={item}
              >
                {item}
              </button>
            ),
          )}
        </div>
        <JobTable jobs={visible} onCancel={cancel} onDownload={download} />
      </Panel>
    </>
  );
}

function Asr({ setError }: { setError: (value: string) => void }) {
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      setResult(
        await apiRequest("control", "/v1/asr/transcriptions/upload", {
          method: "POST",
          body: new FormData(event.currentTarget),
        }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "识别失败");
    } finally {
      setBusy(false);
    }
  }
  async function copyResult() {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(result, null, 2));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("浏览器拒绝了剪贴板访问，请使用下载 JSON");
    }
  }
  function downloadResult() {
    if (result)
      downloadBlob(
        new Blob([JSON.stringify(result, null, 2)], {
          type: "application/json",
        }),
        "asr-result.json",
      );
  }
  return (
    <>
      <PageHeading
        eyebrow="FASTER-WHISPER · GPU0"
        title="语音识别"
        description="上传音频或视频，获取文本、时间轴和语言信息。"
      />
      <div className="two-column">
        <Panel title="新建识别" eyebrow="TRANSCRIBE">
          <form className="stack-form" onSubmit={submit}>
            <label>
              音视频文件
              <input
                name="file"
                type="file"
                accept="audio/*,video/*,.wav,.mp3,.m4a,.aac,.flac,.ogg,.mp4,.mov,.mkv,.webm"
                required
              />
            </label>
            <div className="field-row">
              <label>
                语言提示
                <Select name="language" defaultValue="">
                  <option value="">自动检测</option>
                  <option value="zh">中文</option>
                  <option value="en">英语</option>
                  <option value="th">泰语</option>
                  <option value="ja">日语</option>
                </Select>
              </label>
              <label>
                Beam size
                <input
                  name="beam_size"
                  type="number"
                  min="1"
                  max="10"
                  defaultValue="5"
                />
              </label>
            </div>
            <button className="primary-button" disabled={busy}>
              {busy ? "正在识别…" : "开始识别"}
            </button>
          </form>
        </Panel>
        <Panel title="识别结果" eyebrow="RESULT">
          {result && (
            <div className="row-actions">
              <button onClick={() => void copyResult()}>
                {copied ? "已复制" : "复制 JSON"}
              </button>
              <button onClick={downloadResult}>下载 JSON</button>
            </div>
          )}
          <ResultBox value={result} empty="提交文件后在这里查看结果。" />
        </Panel>
      </div>
    </>
  );
}

function Tts({
  voices,
  providers,
  setError,
}: {
  voices: VoiceProfile[];
  providers: Array<Record<string, unknown>>;
  setError: (value: string) => void;
}) {
  const [audioUrl, setAudioUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState<TtsJob | null>(null);
  const [hasReference, setHasReference] = useState(false);
  useEffect(
    () => () => {
      if (audioUrl) URL.revokeObjectURL(audioUrl);
    },
    [audioUrl],
  );
  useEffect(() => {
    if (!job || ["succeeded", "failed", "cancelled"].includes(job.status))
      return;
    const timer = window.setTimeout(async () => {
      try {
        setJob(
          await apiRequest<TtsJob>("control", `/v2/tts/jobs/${job.job_id}`),
        );
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "异步任务查询失败");
      }
    }, 2500);
    return () => window.clearTimeout(timer);
  }, [job, setError]);
  async function playPcm(response: Response) {
    if (!response.ok)
      throw new Error((await response.json()).detail || "流式语音合成失败");
    if (!response.body) throw new Error("浏览器不支持流式响应");
    const context = new AudioContext({ sampleRate: 48000 });
    const reader = response.body.getReader();
    let pending = new Uint8Array();
    let startAt = context.currentTime + 0.08;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      const merged = new Uint8Array(pending.length + (value?.length || 0));
      merged.set(pending);
      if (value) merged.set(value, pending.length);
      const usable = merged.length - (merged.length % 2);
      pending = merged.slice(usable);
      if (!usable) continue;
      const view = new DataView(merged.buffer, merged.byteOffset, usable);
      const samples = new Float32Array(usable / 2);
      for (let index = 0; index < samples.length; index += 1)
        samples[index] = view.getInt16(index * 2, true) / 32768;
      const buffer = context.createBuffer(1, samples.length, 48000);
      buffer.copyToChannel(samples, 0);
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(context.destination);
      startAt = Math.max(startAt, context.currentTime + 0.04);
      source.start(startAt);
      startAt += buffer.duration;
    }
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const submitter = (event.nativeEvent as SubmitEvent)
      .submitter as HTMLButtonElement | null;
    const data = submitter ? new FormData(form, submitter) : new FormData(form);
    const reference = data.get("reference_audio");
    const cloning = reference instanceof File && reference.size > 0;
    const mode = String(data.get("mode") || "sync");
    const text = String(data.get("text") || "");
    setBusy(true);
    setError("");
    try {
      if (mode !== "async" && text.length > 5000)
        throw new Error("超过5000字符请使用异步任务；异步最多支持20000字符");
      if (cloning) {
        if (data.get("mode") === "async" || data.get("mode") === "stream")
          throw new Error(
            "本地参考音频克隆暂使用同步合成；URL参考音频可调用公开流式接口",
          );
        const response = await fetch(
          `${BASE_PATH}/api/proxy/control/v2/tts/speech/upload`,
          { method: "POST", body: data },
        );
        if (!response.ok)
          throw new Error((await response.json()).detail || "语音克隆失败");
        if (audioUrl) URL.revokeObjectURL(audioUrl);
        setAudioUrl(URL.createObjectURL(await response.blob()));
        return;
      }
      const body = {
        text,
        language: data.get("language"),
        voice_profile_id: data.get("voice_profile_id"),
        provider: data.get("provider"),
        emotion: data.get("emotion") || null,
        emotion_enhance: data.get("emotion_enhance") === "true",
        quality_mode:
          mode === "async"
            ? "standard"
            : data.get("quality_mode") || "standard",
        clone_mode: data.get("clone_mode") || "auto",
        emotion_strategy: data.get("emotion_strategy") || "auto",
        prosody: {
          speed: formNumber(data, "speed", 1),
          volume: formNumber(data, "volume", 1),
          pitch: formNumber(data, "pitch", 1),
        },
        audio: { format: "wav", sample_rate: 48000, channels: 1 },
        timing: { tolerance_ms: 200 },
        metadata: {},
      };
      if (data.get("mode") === "stream") {
        await playPcm(
          await fetch(`${BASE_PATH}/api/proxy/control/v2/tts/speech/stream`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(body),
          }),
        );
        return;
      }
      if (data.get("mode") === "async") {
        setJob(
          await apiRequest<TtsJob>("control", "/v2/tts/jobs", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              ...body,
              idempotency_key: crypto.randomUUID(),
            }),
          }),
        );
        return;
      }
      const response = await fetch(
        `${BASE_PATH}/api/proxy/control/v2/tts/speech`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (!response.ok)
        throw new Error((await response.json()).detail || "合成失败");
      if (audioUrl) URL.revokeObjectURL(audioUrl);
      setAudioUrl(URL.createObjectURL(await response.blob()));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "合成失败");
    } finally {
      setBusy(false);
    }
  }
  async function downloadAsyncAudio() {
    if (!job) return;
    try {
      const response = await fetch(
        `${BASE_PATH}/api/proxy/control/v2/tts/jobs/${job.job_id}/audio`,
      );
      if (!response.ok) throw new Error("异步音频下载失败");
      downloadBlob(await response.blob(), `${job.job_id}.wav`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "异步音频下载失败");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="UNIFIED TTS · GPU1"
        title="语音合成"
        description="支持VoxCPM2跨语言克隆、情绪控制、长文本分段、真实语速/音量/音高和PCM实时播放。"
      />
      <div className="two-column wide-left">
        <Panel title="文本转语音" eyebrow="SYNTHESIZE">
          <form className="stack-form" onSubmit={submit}>
            <label>
              合成文本
              <textarea
                name="text"
                rows={6}
                maxLength={20000}
                placeholder="输入需要合成的文本；超过5000字符请选择异步任务…"
                required
              />
              <small>
                同步与流式最多5000字符；异步任务最多20000字符并自动分段、合并和上传OSS。
              </small>
            </label>
            <div className="field-row">
              <label>
                语言
                <Select name="language" defaultValue="auto">
                  <option value="auto">自动识别</option>
                  <option value="zh">中文</option>
                  <option value="en">英语</option>
                  <option value="es">西班牙语</option>
                  <option value="it">意大利语</option>
                  <option value="ja">日语</option>
                  <option value="th">泰语</option>
                </Select>
              </label>
              <label>
                音色
                <Select name="voice_profile_id" defaultValue="default">
                  {voices.length ? (
                    voices.map((voice) => (
                      <option
                        value={voice.voice_profile_id}
                        key={voice.voice_profile_id}
                      >
                        {voice.display_name}
                      </option>
                    ))
                  ) : (
                    <option value="default">默认音色</option>
                  )}
                </Select>
              </label>
              <label>
                质量模式
                <Select name="quality_mode" defaultValue="standard">
                  <option value="standard">标准（按需质量重试）</option>
                  <option value="strict">严格·最多六候选</option>
                </Select>
              </label>
              <label>
                Provider
                <Select
                  name="provider"
                  defaultValue="auto"
                  disabled={hasReference}
                >
                  <option value="auto">自动路由</option>
                  <option value="voxcpm2">VoxCPM2</option>
                  <option value="doubao">豆包</option>
                  <option value="elevenlabs">ElevenLabs</option>
                </Select>
              </label>
            </div>
            <label>
              参考音频（可选）
              <input
                name="reference_audio"
                type="file"
                accept="audio/*,.wav,.mp3,.m4a,.aac,.flac,.ogg"
                onChange={(event) =>
                  setHasReference(Boolean(event.currentTarget.files?.[0]))
                }
              />
              <small>
                {hasReference
                  ? "已启用 VoxCPM2 深度语音克隆；跨语言会自动改用隔离参考模式并选取代表性5秒。"
                  : "不选择时保持普通 TTS 自动路由。"}
              </small>
            </label>
            {hasReference && (
              <>
                <label>
                  参考音频文本（可选）
                  <textarea
                    name="prompt_text"
                    rows={2}
                    maxLength={5000}
                    placeholder="同语言Ultimate可提升相似度；跨语言仅用于检测和审计，不送入continuation"
                  />
                </label>
                <div className="field-row">
                  <label>
                    克隆模式
                    <Select name="clone_mode" defaultValue="auto">
                      <option value="auto">自动（推荐）</option>
                      <option value="controllable">隔离参考</option>
                      <option value="ultimate">Ultimate</option>
                    </Select>
                  </label>
                  <label>
                    情绪策略
                    <Select name="emotion_strategy" defaultValue="auto">
                      <option value="auto">自动（推荐）</option>
                      <option value="inherit">继承参考</option>
                      <option value="force">强制目标情绪</option>
                    </Select>
                  </label>
                </div>
              </>
            )}
            <label>
              情绪与表达（可选）
              <input
                name="emotion"
                maxLength={200}
                placeholder="例如：真诚、温暖、有感染力，重音清晰"
              />
            </label>
            <label className="toggle-row">
              <span>
                <strong>豆包情绪增强</strong>
                <small>
                  仅在需要强制改变参考情绪时生成节奏、停顿和重音指令。
                </small>
              </span>
              <input name="emotion_enhance" type="checkbox" value="true" />
            </label>
            <div className="range-grid">
              <label>
                语速
                <input
                  name="speed"
                  type="range"
                  min="0.5"
                  max="2"
                  step="0.1"
                  defaultValue="1"
                />
              </label>
              <label>
                音量
                <input
                  name="volume"
                  type="range"
                  min="0.1"
                  max="2"
                  step="0.1"
                  defaultValue="1"
                />
              </label>
              <label>
                音高
                <input
                  name="pitch"
                  type="range"
                  min="0.5"
                  max="2"
                  step="0.1"
                  defaultValue="1"
                />
              </label>
            </div>
            <div className="form-actions">
              <button
                className="primary-button"
                name="mode"
                value="sync"
                disabled={busy}
              >
                {busy
                  ? "处理中…"
                  : hasReference
                    ? "深度克隆并试听"
                    : "同步合成试听"}
              </button>
              <button
                className="secondary-button"
                name="mode"
                value="stream"
                disabled={busy || hasReference}
              >
                实时流式播放
              </button>
              <button
                className="secondary-button"
                name="mode"
                value="async"
                disabled={busy || hasReference}
              >
                提交异步任务
              </button>
            </div>
            {audioUrl && (
              <>
                <audio
                  className="audio-player"
                  src={audioUrl}
                  controls
                  autoPlay
                />
                <a
                  className="secondary-button"
                  href={audioUrl}
                  download="tts-speech.wav"
                >
                  下载试听音频
                </a>
              </>
            )}
            {job && (
              <div className="note-card">
                <b>异步任务 · {job.job_id}</b>
                <p>
                  状态：
                  <StatusBadge status={job.status} />
                  {job.error ? ` · ${job.error}` : ""}
                </p>
                {job.status === "succeeded" && (
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => void downloadAsyncAudio()}
                  >
                    下载异步音频
                  </button>
                )}
              </div>
            )}
          </form>
        </Panel>
        <Panel title="Provider 状态" eyebrow="ROUTING">
          <div className="provider-list">
            {providers.length ? (
              providers.map((provider, index) => (
                <article key={index}>
                  <strong>
                    {String(
                      provider.name ||
                        provider.provider ||
                        `Provider ${index + 1}`,
                    )}
                  </strong>
                  <StatusBadge
                    status={String(provider.status || "configured")}
                  />
                  <pre>{JSON.stringify(provider, null, 2)}</pre>
                </article>
              ))
            ) : (
              <p className="empty-note">加载中…</p>
            )}
          </div>
        </Panel>
      </div>
    </>
  );
}

function Ocr({ setError }: { setError: (value: string) => void }) {
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [subtitleResult, setSubtitleResult] = useState<Record<
    string,
    unknown
  > | null>(null);
  const [busy, setBusy] = useState(false);
  async function submitImages(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const response = await fetch(`${BASE_PATH}/api/tools/ocr`, {
        method: "POST",
        body: new FormData(event.currentTarget),
      });
      const body = await response.json();
      if (!response.ok)
        throw new Error(
          typeof body.detail === "string"
            ? body.detail
            : JSON.stringify(body.detail),
        );
      setResult(body);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "OCR失败");
    } finally {
      setBusy(false);
    }
  }
  async function submitSubtitle(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    const data = new FormData(event.currentTarget);
    try {
      const inputPath = String(data.get("input_path") || "");
      const outputDir = String(data.get("output_dir") || "");
      if (!inputPath.startsWith("/home/donxu/ai-centre/runtime/"))
        throw new Error("输入必须位于允许的 runtime 目录");
      if (outputDir && !outputDir.startsWith("/home/donxu/ai-centre/runtime/"))
        throw new Error("输出必须位于允许的 runtime 目录");
      const coarse = Number(data.get("coarse_interval_seconds"));
      setSubtitleResult(
        await apiRequest("control", "/internal/admin/subtitle/detect", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            input_path: inputPath,
            output_dir: outputDir || null,
            source_lang_hint: data.get("language") || null,
            mode: data.get("mode"),
            export_debug_video: data.get("export_debug_video") === "true",
            asr_segments: [],
            config: {
              coarse_interval_seconds:
                Number.isFinite(coarse) && coarse > 0 ? coarse : null,
              boundary_window_seconds: formNumber(
                data,
                "boundary_window_seconds",
                0.3,
              ),
              center_tolerance_top: formNumber(
                data,
                "center_tolerance_top",
                0.14,
              ),
              center_tolerance_bottom: formNumber(
                data,
                "center_tolerance_bottom",
                0.1,
              ),
              horizontal_padding_ratio: formNumber(
                data,
                "horizontal_padding_ratio",
                0.0278,
              ),
              max_tracks_per_half: formNumber(data, "max_tracks_per_half", 1),
              min_ocr_score: formNumber(data, "min_ocr_score", 0.45),
              scene_cut_threshold: formNumber(
                data,
                "scene_cut_threshold",
                0.42,
              ),
            },
          }),
        }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "字幕检测失败");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="PADDLEOCR · DUAL GPU"
        title="OCR 与字幕"
        description="批量图片识别走负载均衡网关；字幕检测使用受限服务器路径。"
      />
      <div className="two-column">
        <Panel title="图片批量 OCR" eyebrow="IMAGE OCR">
          <form className="stack-form" onSubmit={submitImages}>
            <label>
              图片（最多20张）
              <input
                name="images"
                type="file"
                accept="image/png,image/jpeg,image/webp,image/bmp"
                multiple
                required
              />
            </label>
            <label>
              语言提示
              <Select name="source_lang_hint" defaultValue="">
                <option value="">自动</option>
                <option value="zh">中文</option>
                <option value="en">英语</option>
                <option value="th">泰语</option>
              </Select>
            </label>
            <button className="primary-button" disabled={busy}>
              {busy ? "识别中…" : "开始 OCR"}
            </button>
          </form>
          <ResultBox value={result} empty="识别结果将在这里显示。" />
        </Panel>
        <Panel title="字幕事件检测" eyebrow="ADVANCED">
          <form className="stack-form" onSubmit={submitSubtitle}>
            <label>
              服务器视频路径
              <input
                name="input_path"
                placeholder="/home/donxu/ai-centre/runtime/.../video.mp4"
                required
              />
            </label>
            <label>
              输出目录（可选）
              <input name="output_dir" placeholder="自动创建" />
            </label>
            <div className="field-row">
              <label>
                模式
                <Select name="mode" defaultValue="balanced">
                  <option value="fast">快速</option>
                  <option value="balanced">平衡</option>
                  <option value="accurate">精确</option>
                </Select>
              </label>
              <label>
                语言
                <input name="language" placeholder="zh / en / th" />
              </label>
            </div>
            <details>
              <summary>高级检测参数</summary>
              <div className="field-row">
                <label>
                  粗采样间隔
                  <input
                    name="coarse_interval_seconds"
                    type="number"
                    min="0.1"
                    step="0.1"
                    placeholder="自动"
                  />
                </label>
                <label>
                  边界窗口
                  <input
                    name="boundary_window_seconds"
                    type="number"
                    min="0"
                    step="0.05"
                    defaultValue="0.3"
                  />
                </label>
                <label>
                  OCR 最低分
                  <input
                    name="min_ocr_score"
                    type="number"
                    min="0"
                    max="1"
                    step="0.05"
                    defaultValue="0.45"
                  />
                </label>
              </div>
              <div className="field-row">
                <label>
                  上半区容差
                  <input
                    name="center_tolerance_top"
                    type="number"
                    min="0"
                    max="1"
                    step="0.01"
                    defaultValue="0.14"
                  />
                </label>
                <label>
                  下半区容差
                  <input
                    name="center_tolerance_bottom"
                    type="number"
                    min="0"
                    max="1"
                    step="0.01"
                    defaultValue="0.1"
                  />
                </label>
                <label>
                  水平留白比
                  <input
                    name="horizontal_padding_ratio"
                    type="number"
                    min="0"
                    max="1"
                    step="0.001"
                    defaultValue="0.0278"
                  />
                </label>
              </div>
              <div className="field-row">
                <label>
                  每半区轨道数
                  <input
                    name="max_tracks_per_half"
                    type="number"
                    min="1"
                    max="10"
                    step="1"
                    defaultValue="1"
                  />
                </label>
                <label>
                  场景切换阈值
                  <input
                    name="scene_cut_threshold"
                    type="number"
                    min="0"
                    max="1"
                    step="0.01"
                    defaultValue="0.42"
                  />
                </label>
              </div>
            </details>
            <label className="toggle-row">
              <span>
                <strong>导出调试视频</strong>
                <small>同时产出审核与 QA 文件</small>
              </span>
              <input
                name="export_debug_video"
                type="checkbox"
                value="true"
                defaultChecked
              />
            </label>
            <button className="secondary-button">运行字幕检测</button>
          </form>
          <ResultBox
            value={subtitleResult}
            empty="高级任务会生成事件JSON、审核页和调试视频。"
          />
        </Panel>
      </div>
    </>
  );
}

function Face({
  setError,
  showNotice,
}: {
  setError: (value: string) => void;
  showNotice: (value: string) => void;
}) {
  const [jobId, setJobId] = useState("");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      const response = await apiRequest<Record<string, unknown>>(
        "control",
        "/v1/face-mosaic/jobs",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            source_uri: data.get("source_uri"),
            filename: data.get("filename") || "face_mosaic.mp4",
            callback_url: data.get("callback_url") || null,
            metadata: {},
          }),
        },
      );
      setResult(response);
      setJobId(String(response.job_id || ""));
      showNotice("人脸处理任务已提交");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "提交失败");
    }
  }
  async function query() {
    if (!jobId) return;
    try {
      setResult(await apiRequest("control", `/v1/face-mosaic/jobs/${jobId}`));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "查询失败");
    }
  }
  async function cancel() {
    if (!jobId || !window.confirm("确认取消这个人脸处理任务？")) return;
    try {
      setResult(
        await apiRequest("control", `/v1/face-mosaic/jobs/${jobId}/cancel`, {
          method: "POST",
        }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消失败");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="FACE MOSAIC · CELERY"
        title="人脸处理"
        description="提交对象存储或可访问URL，异步完成人脸检测与马赛克处理。"
      />
      <div className="two-column">
        <Panel title="创建任务" eyebrow="NEW FACE JOB">
          <form className="stack-form" onSubmit={submit}>
            <label>
              来源 URI
              <input
                name="source_uri"
                type="url"
                placeholder="https://.../source.mp4"
                required
              />
            </label>
            <label>
              输出文件名
              <input name="filename" defaultValue="face_mosaic.mp4" />
            </label>
            <label>
              回调 URL（可选）
              <input
                name="callback_url"
                type="url"
                placeholder="https://.../callback"
              />
            </label>
            <button className="primary-button">提交处理</button>
          </form>
        </Panel>
        <Panel title="任务查询" eyebrow="JOB STATUS">
          <div className="inline-query">
            <input
              value={jobId}
              onChange={(event) => setJobId(event.target.value)}
              placeholder="任务 UUID"
            />
            <button onClick={() => void query()}>查询</button>
            <button className="danger-link" onClick={() => void cancel()}>
              取消
            </button>
          </div>
          <ResultBox value={result} empty="提交任务或输入已有任务 ID。" />
        </Panel>
      </div>
    </>
  );
}

function VideoDepth({
  setError,
  showNotice,
}: {
  setError: (value: string) => void;
  showNotice: (value: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [jobId, setJobId] = useState("");
  const [result, setResult] = useState<unknown>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const data = new FormData(event.currentTarget);
    const wait = data.get("mode") === "wait";
    try {
      const response = await apiRequest<Record<string, unknown>>(
        "control",
        `/v1/video-depth/jobs${wait ? "/wait" : ""}`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            source_uri: data.get("source_uri"),
            filename: data.get("filename") || "depth.mp4",
            version: data.get("version") || "da2",
            model: data.get("model") || "small",
            input_size: formNumber(data, "input_size", 518),
            max_resolution: formNumber(data, "max_resolution", 960),
            target_fps: formNumber(data, "target_fps", -1),
          }),
        },
      );
      setResult(response);
      if (response.job_id) setJobId(String(response.job_id));
      showNotice(wait ? "深度推理已完成" : "深度推理任务已提交");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "提交失败");
    } finally {
      setBusy(false);
    }
  }
  async function query() {
    if (!jobId) return;
    try {
      setResult(await apiRequest("control", `/v1/video-depth/jobs/${jobId}`));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "查询失败");
    }
  }
  async function cancel() {
    if (!jobId || !window.confirm("确认取消这个视频深度任务？")) return;
    try {
      setResult(
        await apiRequest("control", `/v1/video-depth/jobs/${jobId}/cancel`, {
          method: "POST",
        }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消失败");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="VIDEO DEPTH · DA2 / DA3"
        title="视频深度推理"
        description="选择 DA2 或 DA3 的 Small/Base 模型；Small 显存更低，Base 细节更强。任务可并发排队，GPU 推理受显存保护。"
      />
      <div className="two-column">
        <Panel title="创建深度任务" eyebrow="DEPTH JOB">
          <form className="stack-form" onSubmit={submit}>
            <label>
              来源视频 URL
              <input
                name="source_uri"
                type="url"
                placeholder="https://.../source.mp4"
                required
              />
            </label>
            <label>
              输出文件名
              <input name="filename" defaultValue="depth.mp4" />
            </label>
            <div className="field-row">
              <label>
                模型版本
                <Select name="version" defaultValue="da2">
                  <option value="da2">DA2 / Video Depth Anything</option>
                  <option value="da3">Depth Anything 3</option>
                </Select>
              </label>
              <label>
                模型规模
                <Select name="model" defaultValue="small">
                  <option value="small">Small（低显存）</option>
                  <option value="base">Base（高质量）</option>
                </Select>
              </label>
            </div>
            <div className="field-row">
              <label>
                模型输入尺寸
                <input
                  name="input_size"
                  type="number"
                  min="224"
                  max="756"
                  step="14"
                  defaultValue="518"
                />
              </label>
              <label>
                最大视频边长
                <input
                  name="max_resolution"
                  type="number"
                  min="224"
                  max="1920"
                  defaultValue="960"
                />
              </label>
              <label>
                目标帧率
                <input
                  name="target_fps"
                  type="number"
                  min="-1"
                  max="60"
                  step="0.1"
                  defaultValue="-1"
                />
              </label>
            </div>
            <label>
              执行方式
              <Select name="mode" defaultValue="async">
                <option value="async">异步排队</option>
                <option value="wait">高优先级等待结果</option>
              </Select>
            </label>
            <button className="primary-button" disabled={busy}>
              {busy ? "处理中…" : "提交深度推理"}
            </button>
          </form>
        </Panel>
        <Panel title="任务结果" eyebrow="JOB STATUS">
          <div className="inline-query">
            <input
              value={jobId}
              onChange={(event) => setJobId(event.target.value)}
              placeholder="任务 UUID"
            />
            <button onClick={() => void query()}>查询</button>
            <button className="danger-link" onClick={() => void cancel()}>
              取消
            </button>
          </div>
          <ResultBox value={result} empty="提交任务或输入已有任务 ID。" />
        </Panel>
      </div>
    </>
  );
}

function VideoUpscale({
  setError,
  showNotice,
}: {
  setError: (value: string) => void;
  showNotice: (value: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [jobId, setJobId] = useState("");
  const [result, setResult] = useState<unknown>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    const wait = form.get("mode") === "wait";
    try {
      const response = await apiRequest<Record<string, unknown>>(
        "control",
        `/v1/video-upscale/jobs${wait ? "/wait" : ""}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            source_uri: form.get("source_uri"),
            provider: form.get("provider"),
            max_resolution: Number(form.get("max_resolution")),
          }),
        },
      );
      setResult(response);
      if (typeof response.job_id === "string") setJobId(response.job_id);
      showNotice("视频超分任务已提交");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "提交失败");
    } finally {
      setBusy(false);
    }
  }
  async function query() {
    if (!jobId) return;
    try {
      setResult(await apiRequest("control", `/v1/video-upscale/jobs/${jobId}`));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "查询失败");
    }
  }
  async function cancel() {
    if (!jobId || !window.confirm("确认取消这个视频超分任务？")) return;
    try {
      setResult(
        await apiRequest("control", `/v1/video-upscale/jobs/${jobId}/cancel`, {
          method: "POST",
        }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消失败");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="VIDEO UPSCALE · SMART FALLBACK"
        title="视频超分"
        description="统一接入 FlashVSR V2、FlashVSR 与 SeedVR2。智能渠道失败时自动切换下一个 Provider，也可指定单一渠道。"
      />
      <div className="two-column">
        <Panel title="创建超分任务" eyebrow="UPSCALE JOB">
          <form className="stack-form" onSubmit={submit}>
            <label>
              来源视频 URL
              <input
                name="source_uri"
                type="url"
                placeholder="https://.../source.mp4"
                required
              />
            </label>
            <div className="field-row">
              <label>
                超分渠道
                <Select name="provider" defaultValue="auto">
                  <option value="auto">智能渠道（自动故障切换）</option>
                  <option value="flashvsr_v2">FlashVSR V2</option>
                  <option value="flashvsr">FlashVSR</option>
                  <option value="seedvr2">SeedVR2</option>
                </Select>
              </label>
              <label>
                目标最大分辨率
                <input
                  name="max_resolution"
                  type="number"
                  min="480"
                  max="3840"
                  defaultValue="1920"
                />
              </label>
            </div>
            <label>
              执行方式
              <Select name="mode" defaultValue="async">
                <option value="async">异步排队</option>
                <option value="wait">高优先级等待结果</option>
              </Select>
            </label>
            <button className="primary-button" disabled={busy}>
              {busy ? "处理中…" : "提交视频超分"}
            </button>
          </form>
        </Panel>
        <Panel title="任务结果" eyebrow="JOB STATUS">
          <div className="inline-query">
            <input
              value={jobId}
              onChange={(event) => setJobId(event.target.value)}
              placeholder="任务 UUID"
            />
            <button onClick={() => void query()}>查询</button>
            <button className="danger-link" onClick={() => void cancel()}>
              取消
            </button>
          </div>
          <ResultBox value={result} empty="提交任务或输入已有任务 ID。" />
        </Panel>
      </div>
      <RecentTasks service="upscale" setError={setError} />
    </>
  );
}

function AudioSeparation({
  setError,
  showNotice,
}: {
  setError: (value: string) => void;
  showNotice: (value: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [jobId, setJobId] = useState("");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const data = new FormData(event.currentTarget);
    const wait = data.get("mode") === "wait";
    try {
      const response = await apiRequest<Record<string, unknown>>(
        "control",
        `/v1/audio-separation/jobs${wait ? "/wait" : ""}`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            source_uri: data.get("source_uri"),
            model: "bandit-v2-multilingual",
            filename_prefix: data.get("filename_prefix") || "separated",
            metadata: {},
          }),
        },
      );
      setResult(response);
      if (response.job_id) setJobId(String(response.job_id));
      showNotice(wait ? "四轨分离已完成" : "音频分离任务已提交");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "提交失败");
    } finally {
      setBusy(false);
    }
  }
  async function query() {
    if (!jobId) return;
    try {
      setResult(
        await apiRequest("control", `/v1/audio-separation/jobs/${jobId}`),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "查询失败");
    }
  }
  async function cancel() {
    if (!jobId || !window.confirm("确认取消这个音频分离任务？")) return;
    try {
      setResult(
        await apiRequest(
          "control",
          `/v1/audio-separation/jobs/${jobId}/cancel`,
          { method: "POST" },
        ),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消失败");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="BANDIT V2 · GPU1"
        title="音频分离"
        description="从音频或视频URL提取影视对白、音乐、音效，并生成合成背景轨；结果自动上传OSS。"
      />
      <div className="two-column">
        <Panel title="创建分离任务" eyebrow="SEPARATION JOB">
          <form className="stack-form" onSubmit={submit}>
            <label>
              来源音频或视频 URL
              <input
                name="source_uri"
                type="url"
                placeholder="https://.../source.mp4"
                required
              />
            </label>
            <label>
              结果文件名前缀
              <input name="filename_prefix" defaultValue="separated" />
            </label>
            <label>
              模型
              <input value="Bandit v2 DnR v3 Multilingual" readOnly />
            </label>
            <label>
              执行方式
              <Select name="mode" defaultValue="async">
                <option value="async">异步排队</option>
                <option value="wait">高优先级等待四轨结果</option>
              </Select>
            </label>
            <button className="primary-button" disabled={busy}>
              {busy ? "处理中…" : "提交音频分离"}
            </button>
          </form>
        </Panel>
        <Panel title="任务结果" eyebrow="JOB STATUS">
          <div className="inline-query">
            <input
              value={jobId}
              onChange={(event) => setJobId(event.target.value)}
              placeholder="任务 UUID"
            />
            <button onClick={() => void query()}>查询</button>
            <button className="danger-link" onClick={() => void cancel()}>
              取消
            </button>
          </div>
          <ResultBox
            value={result}
            empty="完成后返回 speech、music、sfx、background 四个OSS URL。"
          />
        </Panel>
      </div>
    </>
  );
}

function MiniMaxH3({
  setError,
  showNotice,
}: {
  setError: (value: string) => void;
  showNotice: (value: string) => void;
}) {
  const [jobs, setJobs] = useState<H3Job[]>([]);
  const [workers, setWorkers] = useState<H3Worker[]>([]);
  const [workerSummary, setWorkerSummary] = useState<H3WorkerSummary | null>(
    null,
  );
  const [pool, setPool] = useState<H3PoolStatus | null>(null);
  const [unavailableOpen, setUnavailableOpen] = useState(false);
  const [unavailableDeployments, setUnavailableDeployments] = useState<
    H3ManagedDeployment[]
  >([]);
  const [unavailablePage, setUnavailablePage] = useState(1);
  const [unavailableTotal, setUnavailableTotal] = useState(0);
  const [unavailableStatus, setUnavailableStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");
  const [detail, setDetail] = useState<H3Job | null>(null);
  const [confirm, setConfirm] = useState<
    | { kind: "worker"; worker: H3Worker }
    | { kind: "deployment"; deployment: H3ManagedDeployment }
    | { kind: "job"; job: H3Job }
    | null
  >(null);
  const [modalBusy, setModalBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [jobResult, workerResult, poolResult] = await Promise.allSettled([
        apiRequest<{ items: H3Job[] }>(
          "control",
          "/internal/admin/h3/jobs?limit=100",
        ),
        apiRequest<{ workers: H3Worker[]; summary: H3WorkerSummary }>(
          "control",
          "/internal/admin/h3/workers",
        ),
        apiRequest<H3PoolStatus>("control", "/internal/admin/h3/pool"),
      ]);
      if (jobResult.status === "fulfilled") setJobs(jobResult.value.items);
      if (workerResult.status === "fulfilled") {
        setWorkers(workerResult.value.workers);
        setWorkerSummary(workerResult.value.summary);
      }
      if (poolResult.status === "fulfilled") setPool(poolResult.value);
      if (
        [jobResult, workerResult, poolResult].every(
          (result) => result.status === "rejected",
        )
      )
        throw new Error("H3状态加载失败");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "H3状态加载失败");
    }
  }, [setError]);

  const loadUnavailable = useCallback(async () => {
    if (!unavailableOpen) return;
    try {
      const query = new URLSearchParams({
        availability: "unavailable",
        page: String(unavailablePage),
        page_size: "20",
      });
      if (unavailableStatus) query.set("status", unavailableStatus);
      const result = await apiRequest<{
        items: H3ManagedDeployment[];
        total: number;
      }>("control", `/internal/admin/h3/pool/deployments?${query}`);
      setUnavailableDeployments(result.items);
      setUnavailableTotal(result.total);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "暂不可用实例加载失败",
      );
    }
  }, [setError, unavailableOpen, unavailablePage, unavailableStatus]);

  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    const timer = window.setInterval(() => void load(), 10000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [load]);

  useEffect(() => {
    if (!unavailableOpen) return;
    const pending = window.setTimeout(() => void loadUnavailable(), 0);
    return () => window.clearTimeout(pending);
  }, [loadUnavailable, unavailableOpen]);

  async function submitJob(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setFormError("");
    const data = new FormData(event.currentTarget);
    try {
      await apiRequest("control", "/v1/video-generations/minimax-h3/jobs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          reference_video_urls: data
            .getAll("reference_video_urls")
            .map(String)
            .map((value) => value.trim())
            .filter(Boolean),
          reference_image_urls: data
            .getAll("reference_image_urls")
            .map(String)
            .map((value) => value.trim())
            .filter(Boolean),
          reference_audio_urls: data
            .getAll("reference_audio_urls")
            .map(String)
            .map((value) => value.trim())
            .filter(Boolean),
          prompt: String(data.get("prompt") || ""),
          duration_seconds: formNumber(data, "duration_seconds", 5),
          resolution: data.get("resolution") || "480p",
          quality: data.get("quality") || "medium",
          aspect_ratio: data.get("aspect_ratio") || "9:16",
          priority: formNumber(data, "priority", 500),
          seed: formNumber(data, "seed", 482901731),
          external_ref: data.get("external_ref") || null,
          metadata: {},
        }),
      });
      showNotice("MiniMax H3任务已进入异步队列");
      await load();
    } catch (reason) {
      const message =
        reason instanceof Error ? reason.message : "H3任务提交失败";
      setFormError(message);
      setError(message);
    } finally {
      setBusy(false);
    }
  }

  async function createWorker(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      await apiRequest("control", "/internal/admin/h3/workers", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          name: data.get("name"),
          base_url: data.get("base_url"),
          enabled: true,
        }),
      });
      form.reset();
      showNotice("Worker已注册，健康检查将在20秒内确认上线");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Worker注册失败");
    }
  }

  async function createManagedWorker(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      await apiRequest("control", "/internal/admin/h3/pool/workers", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          machine_type: data.get("machine_type") || "5090_32g",
          provider_mode: data.get("provider_mode") || "spot",
        }),
      });
      showNotice("共绩 Worker 创建请求已提交，系统会自动注册上线");
      await load();
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "共绩 Worker 创建失败",
      );
    }
  }

  async function savePoolConfig(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      await apiRequest("control", "/internal/admin/h3/pool/config", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          min_workers: Number(data.get("min_workers")),
          max_workers: Number(data.get("max_workers")),
          idle_timeout_seconds: Number(data.get("idle_timeout_minutes")) * 60,
          default_machine_type: data.get("default_machine_type"),
          provider_mode: data.get("provider_mode"),
          spot_estimated_exec_seconds: Math.round(
            Number(data.get("spot_estimated_exec_hours")) * 3600,
          ),
          autoscaling_enabled: data.get("autoscaling_enabled") === "true",
        }),
      });
      showNotice("弹性调度策略已保存");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "策略保存失败");
    }
  }

  async function workerAction(
    worker: H3Worker,
    action: "test" | "drain" | "toggle" | "delete",
  ) {
    try {
      if (action === "delete") {
        setConfirm({ kind: "worker", worker });
        return;
      }
      const path =
        action === "test" || action === "drain"
          ? `/internal/admin/h3/workers/${worker.id}/${action}`
          : `/internal/admin/h3/workers/${worker.id}`;
      await apiRequest(
        "control",
        path,
        action === "toggle"
          ? {
              method: "PATCH",
              headers: { "content-type": "application/json" },
              body: JSON.stringify({
                enabled: !worker.enabled,
                draining: false,
              }),
            }
          : { method: "POST" },
      );
      showNotice(`Worker操作已完成：${action}`);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Worker操作失败");
    }
  }

  async function cancel(job: H3Job) {
    setConfirm({ kind: "job", job });
  }

  async function confirmDestructiveAction() {
    if (!confirm) return;
    setModalBusy(true);
    try {
      if (confirm.kind === "worker") {
        await apiRequest(
          "control",
          `/internal/admin/h3/workers/${confirm.worker.id}`,
          { method: "DELETE" },
        );
        showNotice(`Worker ${confirm.worker.name} 已删除`);
      } else if (confirm.kind === "deployment") {
        await apiRequest(
          "control",
          `/internal/admin/h3/pool/workers/${confirm.deployment.id}`,
          { method: "DELETE" },
        );
        showNotice(`共绩实例 ${confirm.deployment.name} 已永久停止`);
      } else {
        await apiRequest(
          "control",
          `/v1/video-generations/minimax-h3/jobs/${confirm.job.job_id}/cancel`,
          { method: "POST" },
        );
        showNotice("取消请求已提交");
      }
      setConfirm(null);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setModalBusy(false);
    }
  }

  async function openDetail(job: H3Job) {
    try {
      setDetail(
        await apiRequest<H3Job>(
          "control",
          `/internal/admin/h3/jobs/${job.job_id}`,
        ),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "任务详情加载失败");
    }
  }

  const availableWorkers = workers.filter(
    (worker) =>
      worker.enabled &&
      !worker.draining &&
      ["online", "busy"].includes(worker.status),
  );
  const unavailableWorkers = workers.filter(
    (worker) => !availableWorkers.includes(worker),
  );
  const unavailablePages = Math.max(1, Math.ceil(unavailableTotal / 20));
  const deploymentRows = (items: H3ManagedDeployment[]) =>
    items.map((item) => (
      <tr key={item.id}>
        <td>
          <strong>{item.name}</strong>
        </td>
        <td>
          {item.machine_type.replace("_", " · ").toUpperCase()}
          <small>
            {item.provider_mode === "spot" ? "抢占式 Spot" : "弹性部署"}
          </small>
        </td>
        <td>
          <StatusBadge status={item.status} />
          {item.worker_status && <small>Worker: {item.worker_status}</small>}
        </td>
        <td className="mono">{item.provider_task_id || "—"}</td>
        <td>{formatDuration(item.offline_duration_seconds ?? undefined)}</td>
        <td>{formatTime(item.created_at)}</td>
        <td>{item.last_error || "—"}</td>
        <td>
          {!["stopped", "failed"].includes(item.status) ? (
            <button
              className="danger-link"
              onClick={() =>
                setConfirm({ kind: "deployment", deployment: item })
              }
            >
              永久停止
            </button>
          ) : (
            "—"
          )}
        </td>
      </tr>
    ));
  const workerRows = (items: H3Worker[]) =>
    items.map((worker) => (
      <tr key={worker.id}>
        <td>
          <strong>{worker.name}</strong>
          <small>{worker.base_url}</small>
        </td>
        <td>
          <StatusBadge status={worker.draining ? "draining" : worker.status} />
        </td>
        <td>
          {worker.gpu_name || "—"}
          <small>
            {worker.vram_total_bytes
              ? `${((worker.vram_total_bytes - Number(worker.vram_free_bytes || 0)) / 2 ** 30).toFixed(1)} / ${(worker.vram_total_bytes / 2 ** 30).toFixed(1)} GiB`
              : "—"}
          </small>
        </td>
        <td>
          {worker.queue_running}运行 / {worker.queue_pending}等待
        </td>
        <td className="mono">{worker.current_job_id?.slice(0, 8) || "—"}</td>
        <td>
          {["online", "busy"].includes(worker.status)
            ? formatDuration(worker.status_duration_seconds ?? undefined)
            : "—"}
          <small>
            {worker.status === "busy"
              ? "本次忙碌"
              : worker.status === "online"
                ? "持续在线"
                : ""}
          </small>
        </td>
        <td>{formatTime(worker.last_checked_at)}</td>
        <td>
          {worker.status === "offline"
            ? formatDuration(worker.offline_duration_seconds ?? undefined)
            : "—"}
        </td>
        <td>
          <div className="row-actions">
            <button onClick={() => void workerAction(worker, "test")}>
              测试
            </button>
            <button onClick={() => void workerAction(worker, "drain")}>
              排空
            </button>
            <button onClick={() => void workerAction(worker, "toggle")}>
              {worker.enabled ? "停用" : "启用"}
            </button>
            <button
              className="danger-link"
              onClick={() => void workerAction(worker, "delete")}
            >
              删除
            </button>
          </div>
        </td>
      </tr>
    ));

  return (
    <>
      <PageHeading
        eyebrow="MINIMAX H3 · MULTI-WORKER"
        title="MiniMax H3视频生成"
        description="提供单次原子视频生成能力；多段拆分、连续性和业务编排由上层Agent负责。"
        actions={
          <button className="secondary-button" onClick={() => void load()}>
            刷新
          </button>
        }
      />
      <div className="two-column wide-left">
        <Panel title="提交生成任务" eyebrow="ASYNC GENERATION">
          <form className="stack-form" onSubmit={submitJob}>
            <UrlListInput
              name="reference_video_urls"
              label="参考视频 HTTPS URL（可多项）"
              placeholder="https://storage.example.com/reference.mp4"
            />
            <UrlListInput
              name="reference_image_urls"
              label="参考图片 HTTPS URL（可多项）"
              placeholder="https://storage.example.com/reference.png"
            />
            <UrlListInput
              name="reference_audio_urls"
              label="参考音频 HTTPS URL（可多项）"
              placeholder="https://storage.example.com/reference.mp3"
            />
            <small id="h3-reference-help">
              三个参考素材可以全部留空，此时按Prompt进行纯文本视频生成；提供素材时，Prompt中的&lt;Picture
              1&gt;、&lt;Video 1&gt;、&lt;Audio
              1&gt;由业务端按实际素材使用。每个请求只生成一个2～15秒片段。
            </small>
            <div className="field-row">
              <label>
                优先级（1～1000）
                <input
                  name="priority"
                  type="number"
                  min="1"
                  max="1000"
                  defaultValue="500"
                  required
                />
              </label>
              <label>
                业务单号
                <input name="external_ref" maxLength={256} />
              </label>
            </div>
            <label>
              完整Prompt
              <textarea name="prompt" rows={5} required />
            </label>
            <div className="field-row">
              <label>
                质量档位
                <Select name="quality" defaultValue="medium">
                  <option value="low">低 · 快速预览</option>
                  <option value="medium">中 · 标准生产（默认）</option>
                  <option value="high">高 · 质量采样（较慢）</option>
                </Select>
              </label>
              <label>
                清晰度
                <Select name="resolution" defaultValue="720p">
                  <option value="480p">480P（预览）</option>
                  <option value="720p">720P / 原生 768×1344（默认）</option>
                  <option value="1080p">1080P</option>
                </Select>
              </label>
              <label>
                画面比例
                <Select name="aspect_ratio" defaultValue="9:16">
                  <option value="9:16">9:16 竖屏</option>
                  <option value="16:9">16:9 横屏</option>
                  <option value="1:1">1:1 方形</option>
                  <option value="4:3">4:3 横屏</option>
                  <option value="3:4">3:4 竖屏</option>
                </Select>
              </label>
              <label>
                生成时长（秒）
                <input name="duration_seconds" type="number" min="2" max="15" step="0.1" defaultValue="5" required />
              </label>
              <label>
                Seed
                <input
                  name="seed"
                  type="number"
                  min="0"
                  defaultValue="482901731"
                  required
                />
              </label>
            </div>
            <small>
              默认中档、720P、9:16、5秒。低档使用较小内部画布快速预览；高档使用质量采样器，耗时明显增加。每次请求只生成一个2～15秒原子片段。
            </small>
            {formError && (
              <div
                className="form-error"
                id="h3-form-error"
                role="alert"
                aria-live="assertive"
              >
                <b>无法提交：</b>
                {formError}
              </div>
            )}
            <button className="primary-button" type="submit" disabled={busy}>
              {busy ? "提交中…" : "提交异步任务"}
            </button>
          </form>
        </Panel>
        <Panel title="注册Worker" eyebrow="WORKER REGISTRY">
          <form className="stack-form" onSubmit={createWorker}>
            <label>
              Worker名称
              <input
                name="name"
                pattern="[A-Za-z0-9][A-Za-z0-9._-]*"
                required
                placeholder="h3-worker-01"
              />
            </label>
            <label>
              ComfyUI URL
              <input
                name="base_url"
                type="url"
                required
                placeholder="https://worker.example.com/"
              />
            </label>
            <button className="secondary-button">注册并检查</button>
          </form>
          <p className="empty-note">
            Worker地址只在服务端保存，页面及普通日志仅显示脱敏地址。
          </p>
        </Panel>
      </div>
      <div className="two-column wide-left">
        <Panel title="共绩 Worker" eyebrow="SUANLI WORKER POOL">
          <form className="stack-form" onSubmit={createManagedWorker}>
            <label>
              机器类型
              <Select name="machine_type" defaultValue="5090_32g">
                <option value="4090_24g">RTX 4090 · 24G</option>
                <option value="4090_48g">RTX 4090 · 48G</option>
                <option value="5090_32g">RTX 5090 · 32G（默认）</option>
              </Select>
            </label>
            <label>
              供应方式
              <Select
                name="provider_mode"
                defaultValue={pool?.config.provider_mode || "spot"}
              >
                <option value="spot">抢占式 Spot Job（默认）</option>
                <option value="deployment">弹性部署 Deployment</option>
              </Select>
            </label>
            <button className="primary-button" disabled={!pool?.configured}>
              增加 Worker
            </button>
            <small>
              {pool?.configured
                ? "默认创建抢占式5090 Spot Job；系统自动选择有库存区域，等待8188就绪后注册到H3调度。"
                : "服务器尚未配置共绩 API Key。"}
            </small>
          </form>
        </Panel>
        <Panel title="调度策略" eyebrow="AUTO SCALING POLICY">
          {pool && (
            <form
              className="stack-form"
              onSubmit={savePoolConfig}
              key={pool.config.updated_at}
            >
              <div className="field-row">
                <label>
                  最小 Worker 数
                  <input
                    name="min_workers"
                    type="number"
                    min="0"
                    max="100"
                    defaultValue={pool.config.min_workers}
                    required
                  />
                </label>
                <label>
                  最大 Worker 数
                  <input
                    name="max_workers"
                    type="number"
                    min="1"
                    max="100"
                    defaultValue={pool.config.max_workers}
                    required
                  />
                </label>
              </div>
              <label>
                空闲最大存活时间（分钟）
                <input
                  name="idle_timeout_minutes"
                  type="number"
                  min="1"
                  max="10080"
                  defaultValue={Math.round(
                    pool.config.idle_timeout_seconds / 60,
                  )}
                  required
                />
              </label>
              <label>
                默认启动机器
                <Select
                  name="default_machine_type"
                  defaultValue={pool.config.default_machine_type}
                >
                  <option value="4090_24g">RTX 4090 · 24G</option>
                  <option value="4090_48g">RTX 4090 · 48G</option>
                  <option value="5090_32g">RTX 5090 · 32G</option>
                </Select>
              </label>
              <label>
                默认供应方式
                <Select
                  name="provider_mode"
                  defaultValue={pool.config.provider_mode}
                >
                  <option value="spot">抢占式 Spot Job</option>
                  <option value="deployment">弹性部署 Deployment</option>
                </Select>
              </label>
              <label>
                Spot预计运行时间（小时）
                <input
                  name="spot_estimated_exec_hours"
                  type="number"
                  min="1"
                  max="24"
                  step="0.5"
                  defaultValue={pool.config.spot_estimated_exec_seconds / 3600}
                  required
                />
              </label>
              <label className="toggle-row">
                <span>
                  <strong>启用自动扩缩容</strong>
                  <small>
                    未达到最大Worker数时始终保留1台空闲在线Worker；全部忙碌时提前补机，达到上限后任务排队。
                  </small>
                </span>
                <input
                  name="autoscaling_enabled"
                  type="checkbox"
                  value="true"
                  defaultChecked={pool.config.autoscaling_enabled}
                />
              </label>
              <small>
                离线持续时间从第三次健康检查失败起计算；满30分钟且没有运行任务时自动清除。
              </small>
              <button className="secondary-button">保存策略</button>
            </form>
          )}
        </Panel>
      </div>
      <Panel title="可用共绩实例" eyebrow="AVAILABLE PROVIDER INSTANCES">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>机器/供应方式</th>
                <th>状态</th>
                <th>Task ID</th>
                <th>离线持续</th>
                <th>创建时间</th>
                <th>异常</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {!pool?.deployments.length && (
                <tr>
                  <td colSpan={8} className="empty-cell">
                    暂无健康或启动中的共绩实例，自动扩容器会按策略补充。
                  </td>
                </tr>
              )}
              {deploymentRows(pool?.deployments || [])}
            </tbody>
          </table>
        </div>
      </Panel>
      <div className="metric-grid">
        <article className="metric-card accent">
          <span>可用Worker</span>
          <strong>{workerSummary?.online ?? "—"}</strong>
          <small>只统计健康的空闲与忙碌机器</small>
        </article>
        <article className="metric-card">
          <span>空闲 / 忙碌</span>
          <strong>
            {workerSummary
              ? `${workerSummary.idle} / ${workerSummary.busy}`
              : "—"}
          </strong>
          <small>可立即接单 / 正在生成</small>
        </article>
        <article className="metric-card">
          <span>排队任务</span>
          <strong>{pool?.pending_jobs ?? "—"}</strong>
          <small>
            健康容量 {pool?.capacity ?? 0} · 供应商占位{" "}
            {pool?.provider_slots ?? 0}
          </small>
        </article>
        <article className="metric-card">
          <span>暂不可用</span>
          <strong>
            {pool?.unavailable_total ?? unavailableWorkers.length}
          </strong>
          <small>
            Worker {unavailableWorkers.length} · 实例历史{" "}
            {pool?.unavailable_total ?? 0}
          </small>
        </article>
      </div>
      <div className="metric-grid h3-performance-grid">
        <article className="metric-card accent">
          <span>平均有效处理</span>
          <strong>
            {formatDuration(
              pool?.performance.effective_seconds.mean ?? undefined,
            )}
          </strong>
          <small>排除排队与IQR异常长尾</small>
        </article>
        <article className="metric-card">
          <span>有效耗时 P50</span>
          <strong>
            {formatDuration(
              pool?.performance.effective_seconds.p50 ?? undefined,
            )}
          </strong>
          <small>
            {pool?.performance.effective_sample_count ?? 0} 条有效样本
          </small>
        </article>
        <article className="metric-card">
          <span>有效耗时 P95</span>
          <strong>
            {formatDuration(
              pool?.performance.effective_seconds.p95 ?? undefined,
            )}
          </strong>
          <small>用于生产时延预估</small>
        </article>
        <article className="metric-card">
          <span>剔除异常长尾</span>
          <strong>{pool?.performance.outlier_count ?? 0}</strong>
          <small>原始端到端数据仍保留</small>
        </article>
      </div>
      <Panel title="各机型有效耗时" eyebrow="CLEAN LATENCY BY GPU">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>机型</th>
                <th>成功样本</th>
                <th>有效样本</th>
                <th>平均</th>
                <th>P50</th>
                <th>P95</th>
                <th>剔除长尾</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(pool?.performance.by_machine_type || {}).map(
                ([machine, value]) => (
                  <tr key={machine}>
                    <td>
                      <strong>
                        {machine.replaceAll("_", " · ").toUpperCase()}
                      </strong>
                    </td>
                    <td>{value.success_count}</td>
                    <td>{value.effective_seconds.n}</td>
                    <td>
                      {formatDuration(
                        value.effective_seconds.mean ?? undefined,
                      )}
                    </td>
                    <td>
                      {formatDuration(value.effective_seconds.p50 ?? undefined)}
                    </td>
                    <td>
                      {formatDuration(value.effective_seconds.p95 ?? undefined)}
                    </td>
                    <td>{value.outlier_count}</td>
                  </tr>
                ),
              )}
              {!Object.keys(pool?.performance.by_machine_type || {}).length && (
                <tr>
                  <td colSpan={7} className="empty-cell">
                    暂无已完成任务样本
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel title="可用Worker" eyebrow="ONLINE & BUSY">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Worker</th>
                <th>状态</th>
                <th>GPU/显存</th>
                <th>原生队列</th>
                <th>当前任务</th>
                <th>当前状态持续</th>
                <th>最近检查</th>
                <th>离线持续</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {availableWorkers.length === 0 && (
                <tr>
                  <td colSpan={9} className="empty-cell">
                    当前没有健康Worker，自动扩容器会按最小容量和排队情况补充。
                  </td>
                </tr>
              )}
              {workerRows(availableWorkers)}
            </tbody>
          </table>
        </div>
      </Panel>
      <details
        className="unavailable-directory"
        open={unavailableOpen}
        onToggle={(event) => setUnavailableOpen(event.currentTarget.open)}
      >
        <summary>
          暂不可用（Worker {unavailableWorkers.length} · 共绩实例/历史{" "}
          {pool?.unavailable_total ?? 0}）
        </summary>
        <div className="unavailable-directory-content">
          <Panel
            title="暂不可用Worker"
            eyebrow="OFFLINE · DISABLED · INCOMPATIBLE"
          >
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Worker</th>
                    <th>状态</th>
                    <th>GPU/显存</th>
                    <th>原生队列</th>
                    <th>当前任务</th>
                    <th>当前状态持续</th>
                    <th>最近检查</th>
                    <th>离线持续</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {unavailableWorkers.length === 0 && (
                    <tr>
                      <td colSpan={9} className="empty-cell">
                        没有暂不可用Worker
                      </td>
                    </tr>
                  )}
                  {workerRows(unavailableWorkers)}
                </tbody>
              </table>
            </div>
          </Panel>
          <Panel title="共绩实例历史" eyebrow="UNAVAILABLE DEPLOYMENTS">
            <div className="record-filters compact-filters">
              <label>
                状态
                <Select
                  value={unavailableStatus}
                  onChange={(event) => {
                    setUnavailableStatus(event.target.value);
                    setUnavailablePage(1);
                  }}
                >
                  <option value="">全部</option>
                  <option value="unavailable">暂不可用</option>
                  <option value="failed">失败</option>
                  <option value="stopped">已停止</option>
                  <option value="provisioning">启动超时</option>
                </Select>
              </label>
              <span>
                飞书告警：
                {pool?.notifications.configured
                  ? pool.notifications.last_status || "已配置"
                  : "未配置"}
              </span>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>名称</th>
                    <th>机器/供应方式</th>
                    <th>状态</th>
                    <th>Task ID</th>
                    <th>离线持续</th>
                    <th>创建时间</th>
                    <th>异常</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {unavailableDeployments.length === 0 && (
                    <tr>
                      <td colSpan={8} className="empty-cell">
                        没有匹配的暂不可用实例
                      </td>
                    </tr>
                  )}
                  {deploymentRows(unavailableDeployments)}
                </tbody>
              </table>
            </div>
            <div className="record-pagination">
              <span>
                第 {unavailablePage} / {unavailablePages} 页，共{" "}
                {unavailableTotal} 条
              </span>
              <div>
                <button
                  disabled={unavailablePage <= 1}
                  onClick={() => setUnavailablePage(1)}
                >
                  首页
                </button>
                <button
                  disabled={unavailablePage <= 1}
                  onClick={() =>
                    setUnavailablePage((value) => Math.max(1, value - 1))
                  }
                >
                  上一页
                </button>
                <button
                  disabled={unavailablePage >= unavailablePages}
                  onClick={() =>
                    setUnavailablePage((value) =>
                      Math.min(unavailablePages, value + 1),
                    )
                  }
                >
                  下一页
                </button>
                <button
                  disabled={unavailablePage >= unavailablePages}
                  onClick={() => setUnavailablePage(unavailablePages)}
                >
                  末页
                </button>
              </div>
            </div>
          </Panel>
        </div>
      </details>
      <Panel title="H3任务队列" eyebrow="JOBS">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>任务</th>
                <th>优先级</th>
                <th>状态/阶段</th>
                <th>进度</th>
                <th>尝试</th>
                <th>耗时</th>
                <th>创建时间</th>
                <th>结果</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {jobs.length === 0 && (
                <tr>
                  <td colSpan={9} className="empty-cell">
                    暂无H3任务
                  </td>
                </tr>
              )}
              {jobs.map((job) => (
                <tr key={job.job_id}>
                  <td className="mono">{job.job_id.slice(0, 12)}</td>
                  <td>
                    <strong>{job.priority}</strong>
                  </td>
                  <td>
                    <StatusBadge status={job.status} />
                    <small>{job.stage}</small>
                  </td>
                  <td>{job.progress}%</td>
                  <td>{job.attempt_count}/3</td>
                  <td>{formatDuration(job.elapsed_seconds || undefined)}</td>
                  <td>{formatTime(job.created_at)}</td>
                  <td>
                    {job.result_url ? (
                      <a href={job.result_url} target="_blank" rel="noreferrer">
                        预览/下载
                      </a>
                    ) : (
                      job.error || "—"
                    )}
                  </td>
                  <td>
                    <div className="row-actions">
                      <button
                        className="table-link"
                        onClick={() => void openDetail(job)}
                      >
                        查看详情
                      </button>
                      {["queued", "running", "cancel_requested"].includes(
                        job.status,
                      ) && (
                        <button
                          className="danger-link"
                          onClick={() => void cancel(job)}
                        >
                          取消
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      {confirm && (
        <ConfirmModal
          title={
            confirm.kind === "worker"
              ? "删除 Worker"
              : confirm.kind === "deployment"
                ? "永久停止共绩实例"
                : "取消生成任务"
          }
          message={
            confirm.kind === "worker"
              ? `确认删除 ${confirm.worker.name}？历史任务记录会保留，运行中的 Worker 不能删除。`
              : confirm.kind === "deployment"
                ? `确认永久停止 ${confirm.deployment.name}（Task ID ${confirm.deployment.provider_task_id || "尚未分配"}）？共绩算力实例停止后不可恢复；忙碌中的实例不会被停止。`
                : `确认取消任务 ${confirm.job.job_id}？`
          }
          confirmLabel={
            confirm.kind === "worker"
              ? "确认删除"
              : confirm.kind === "deployment"
                ? "永久停止"
                : "确认取消"
          }
          busy={modalBusy}
          onClose={() => setConfirm(null)}
          onConfirm={() => void confirmDestructiveAction()}
        />
      )}
      {detail && (
        <Modal
          title={`H3任务详情 · ${detail.job_id.slice(0, 12)}`}
          onClose={() => setDetail(null)}
        >
          <div className="detail-content">
            <dl className="detail-grid">
              <div>
                <dt>状态</dt>
                <dd>
                  <StatusBadge status={detail.status} />
                </dd>
              </div>
              <div>
                <dt>有效处理</dt>
                <dd>
                  {formatDuration(
                    detail.timing?.effective_processing_seconds ?? undefined,
                  )}
                </dd>
              </div>
              <div>
                <dt>排队等待</dt>
                <dd>
                  {formatDuration(
                    detail.timing?.queue_wait_seconds ?? undefined,
                  )}
                </dd>
              </div>
              <div>
                <dt>转码与OSS</dt>
                <dd>
                  {formatDuration(
                    detail.timing?.render_and_upload_seconds ?? undefined,
                  )}
                </dd>
              </div>
              <div>
                <dt>端到端</dt>
                <dd>
                  {formatDuration(
                    detail.timing?.end_to_end_seconds ??
                      detail.elapsed_seconds ??
                      undefined,
                  )}
                </dd>
              </div>
              <div>
                <dt>阶段</dt>
                <dd>
                  {detail.stage} · {detail.progress}%
                </dd>
              </div>
              <div className="span-2">
                <dt>完整任务ID</dt>
                <dd className="mono">{detail.job_id}</dd>
              </div>
            </dl>
            <section>
              <h3>请求参数</h3>
              <ResultBox value={detail.request} empty="请求参数不可用" />
            </section>
            <section>
              <h3>响应与结果</h3>
              <ResultBox
                value={{
                  status: detail.status,
                  stage: detail.stage,
                  progress: detail.progress,
                  result_url: detail.result_url,
                  part_urls: detail.part_urls,
                  output: detail.output,
                  error: detail.error,
                  timing: detail.timing,
                }}
                empty="暂无响应"
              />
            </section>
            <section>
              <h3>执行尝试</h3>
              <ResultBox value={detail.attempts} empty="暂无执行尝试" />
            </section>
          </div>
        </Modal>
      )}
    </>
  );
}

function SceneDetect({
  setError,
  showNotice,
}: {
  setError: (value: string) => void;
  showNotice: (value: string) => void;
}) {
  const [jobId, setJobId] = useState("");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const data = new FormData(event.currentTarget);
    const wait = data.get("mode") === "wait";
    try {
      const response = await apiRequest<Record<string, unknown>>(
        "control",
        `/v1/video-scenes/jobs${wait ? "/wait" : ""}`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            source_uri: data.get("source_uri"),
            filename: data.get("filename") || "scene.mp4",
            threshold: formNumber(data, "threshold", 27),
            min_scene_len: formNumber(data, "min_scene_len", 15),
            metadata: {},
          }),
        },
      );
      setResult(response);
      setJobId(String(response.job_id || ""));
      showNotice(wait ? "高优先级切片任务已完成" : "视频切片任务已进入队列");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "视频切片失败");
    } finally {
      setBusy(false);
    }
  }
  async function query() {
    if (!jobId) return;
    try {
      setResult(await apiRequest("control", `/v1/video-scenes/jobs/${jobId}`));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "查询失败");
    }
  }
  async function cancel() {
    if (!jobId || !window.confirm("确认取消这个视频切片任务？")) return;
    try {
      setResult(
        await apiRequest("control", `/v1/video-scenes/jobs/${jobId}/cancel`, {
          method: "POST",
        }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消失败");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="SCENEDETECT · CELERY"
        title="视频切片"
        description="按场景变化异步切分视频；等待模式使用更高优先级并直接返回OSS结果。"
      />
      <div className="two-column">
        <Panel title="创建切片任务" eyebrow="SCENE JOB">
          <form className="stack-form" onSubmit={submit}>
            <label>
              来源视频URL
              <input
                name="source_uri"
                type="url"
                placeholder="https://.../source.mp4"
                required
              />
            </label>
            <label>
              输出文件名
              <input name="filename" defaultValue="scene.mp4" />
            </label>
            <div className="field-row">
              <label>
                检测阈值
                <input
                  name="threshold"
                  type="number"
                  min="1"
                  max="255"
                  step="0.5"
                  defaultValue="27"
                />
              </label>
              <label>
                最短场景帧数
                <input
                  name="min_scene_len"
                  type="number"
                  min="1"
                  max="1000"
                  defaultValue="15"
                />
              </label>
              <label>
                执行方式
                <Select name="mode" defaultValue="async">
                  <option value="async">异步排队</option>
                  <option value="wait">高优先级等待结果</option>
                </Select>
              </label>
            </div>
            <button className="primary-button" disabled={busy}>
              {busy ? "处理中…" : "提交视频切片"}
            </button>
          </form>
        </Panel>
        <Panel title="任务结果" eyebrow="JOB STATUS">
          <div className="inline-query">
            <input
              value={jobId}
              onChange={(event) => setJobId(event.target.value)}
              placeholder="任务 UUID"
            />
            <button onClick={() => void query()}>查询</button>
            <button className="danger-link" onClick={() => void cancel()}>
              取消
            </button>
          </div>
          <ResultBox value={result} empty="提交任务或输入已有任务 ID。" />
        </Panel>
      </div>
    </>
  );
}

function PasswordReauthModal({
  busy,
  error,
  onCancel,
  onSubmit,
}: {
  busy: boolean;
  error: string;
  onCancel: () => void;
  onSubmit: (password: string) => void;
}) {
  const [password, setPassword] = useState("");
  return (
    <Modal
      title="验证管理员身份"
      onClose={onCancel}
      footer={
        <>
          <button type="button" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button
            type="submit"
            form="health-reauth-form"
            className="primary-button"
            disabled={busy || !password}
          >
            {busy ? "验证中…" : "验证并继续"}
          </button>
        </>
      }
    >
      <form
        id="health-reauth-form"
        className="stack-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (password) onSubmit(password);
        }}
      >
        <p className="modal-message">
          保存飞书Webhook或发送测试消息前，请重新输入管理员密码。验证结果10分钟内有效。
        </p>
        <label>
          管理员密码
          <input
            autoFocus
            type="password"
            autoComplete="current-password"
            aria-label="管理员密码"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </form>
    </Modal>
  );
}

function HealthMonitorPanel({
  setError,
  showNotice,
}: {
  setError: (value: string) => void;
  showNotice: (value: string) => void;
}) {
  const [days, setDays] = useState("30"),
    [service, setService] = useState("");
  const [items, setItems] = useState<HealthMonitorItem[]>([]);
  const [config, setConfig] = useState<HealthNotificationConfig | null>(null);
  const [notificationEnabled, setNotificationEnabled] = useState(false);
  const [reauthAction, setReauthAction] = useState<"save" | "test" | null>(
    null,
  );
  const [pendingConfig, setPendingConfig] = useState<{
    enabled: boolean;
    webhook_url: string | null;
  } | null>(null);
  const [reauthBusy, setReauthBusy] = useState(false);
  const [reauthError, setReauthError] = useState("");
  const load = useCallback(async () => {
    try {
      const [status, cfg] = await Promise.all([
        apiRequest<{ items: HealthMonitorItem[] }>(
          "control",
          `/internal/admin/health-monitor/status?days=${days}`,
        ),
        apiRequest<HealthNotificationConfig>(
          "control",
          "/internal/admin/health-monitor/config",
        ),
      ]);
      setItems(status.items);
      setConfig(cfg);
      setNotificationEnabled(cfg.enabled);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "鉴活状态加载失败");
    }
  }, [days, setError]);
  useEffect(() => {
    void load();
  }, [load]);
  async function finishReauth(password: string) {
    setReauthBusy(true);
    setReauthError("");
    try {
      const verified = await fetch(`${BASE_PATH}/api/auth/reauth`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (!verified.ok) throw new Error("管理员密码验证失败");
      if (reauthAction === "save" && pendingConfig) {
        const response = await fetch(`${BASE_PATH}/api/health-monitor/config`, {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            ...pendingConfig,
            secret: null,
            clear_secret: true,
          }),
        });
        if (!response.ok)
          throw new Error((await response.json()).detail || "配置保存失败");
        showNotice("飞书鉴活告警配置已加密保存");
      } else if (reauthAction === "test") {
        const response = await fetch(`${BASE_PATH}/api/health-monitor/config`, {
          method: "POST",
        });
        if (!response.ok)
          throw new Error((await response.json()).detail || "测试通知发送失败");
        showNotice("飞书测试通知已发送");
      }
      setReauthAction(null);
      setPendingConfig(null);
      await load();
    } catch (reason) {
      setReauthError(
        reason instanceof Error ? reason.message : "管理员密码验证失败",
      );
    } finally {
      setReauthBusy(false);
    }
  }
  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setPendingConfig({
      enabled: notificationEnabled,
      webhook_url: String(data.get("webhook_url") || "").trim() || null,
    });
    setReauthError("");
    setReauthAction("save");
  }
  function testNotification() {
    setPendingConfig(null);
    setReauthError("");
    setReauthAction("test");
  }
  async function runCheck() {
    try {
      await apiRequest("control", "/internal/admin/health-monitor/run-check", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ level: "l1", target_id: service || null }),
      });
      showNotice("L1鉴活完成");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "鉴活执行失败");
    }
  }
  const shown = service ? items.filter((item) => item.id === service) : items;
  return (
    <section className="health-monitor">
      <div className="section-heading health-heading">
        <span>STATUS & ALERTING</span>
        <h2>告警与鉴活</h2>
        <p>
          每30分钟检查连通性；真实推理按低优先级独立调度。可用率只统计已有真实样本。
        </p>
      </div>
      <div className="analytics-toolbar">
        <label>
          时间范围
          <Select
            value={days}
            onChange={(event) => setDays(event.target.value)}
          >
            <option value="7">最近7天</option>
            <option value="30">最近30天</option>
          </Select>
        </label>
        <label>
          服务
          <Select
            value={service}
            onChange={(event) => setService(event.target.value)}
          >
            <option value="">全部服务</option>
            {items.map((item) => (
              <option value={item.id} key={item.id}>
                {item.name}
              </option>
            ))}
          </Select>
        </label>
        <button className="secondary-button" onClick={() => void runCheck()}>
          立即执行L1鉴活
        </button>
      </div>
      <Panel title="接口可用性" eyebrow="OPENAI-STYLE STATUS">
        <div className="table-wrap">
          <table className="availability-table">
            <thead>
              <tr>
                <th>服务 / 接口</th>
                <th>当前状态</th>
                <th>L1耗时</th>
                <th>L2业务探针</th>
                <th>{days}天可用率</th>
                <th>P50 / P95</th>
                <th>历史状态</th>
                <th>最后探测</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((item) => (
                <tr key={item.id}>
                  <td>
                    <strong>{item.name}</strong>
                    <small>{item.id}</small>
                  </td>
                  <td>
                    <StatusBadge status={item.current_status} />
                    <small>连续失败 {item.consecutive_failures}</small>
                  </td>
                  <td>
                    {item.last_latency_ms == null
                      ? "暂无数据"
                      : `${item.last_latency_ms.toFixed(0)} ms`}
                  </td>
                  <td>
                    {item.last_l2_status || "暂无数据"}
                    <small>{formatTime(item.last_l2_at)}</small>
                  </td>
                  <td>
                    {item.availability == null
                      ? "暂无数据"
                      : `${item.availability.toFixed(2)}%`}
                    <small>样本 {item.sample_count}</small>
                  </td>
                  <td>
                    {item.p50_ms == null
                      ? "暂无数据"
                      : `${item.p50_ms.toFixed(0)} / ${item.p95_ms?.toFixed(0)} ms`}
                  </td>
                  <td>
                    <div className="status-history">
                      {item.history.length ? (
                        item.history
                          .slice(-24)
                          .map((point, index) => (
                            <i
                              key={`${point.at}-${index}`}
                              className={point.ok ? "is-up" : "is-down"}
                            />
                          ))
                      ) : (
                        <span>暂无数据</span>
                      )}
                    </div>
                  </td>
                  <td>{formatTime(item.last_checked_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <div className="two-column">
        <Panel title="飞书Bot" eyebrow="ENCRYPTED CONFIG">
          <form className="stack-form" onSubmit={save}>
            <label className="toggle-row">
              <input
                type="checkbox"
                name="enabled"
                checked={notificationEnabled}
                onChange={(event) => setNotificationEnabled(event.target.checked)}
              />
              启用故障与恢复通知
            </label>
            <label>
              Webhook
              <input
                name="webhook_url"
                type="url"
                placeholder={
                  config?.configured
                    ? `已配置：${config.webhook_host}（留空保持）`
                    : "https://open.feishu.cn/open-apis/bot/v2/hook/..."
                }
              />
            </label>
            <div className="row-actions">
              <button className="primary-button">保存配置</button>
              <button type="button" onClick={() => void testNotification()}>
                发送测试消息
              </button>
            </div>
          </form>
        </Panel>
        <Panel title="通知状态" eyebrow="DELIVERY">
          <dl className="settings-list">
            <div>
              <dt>配置</dt>
              <dd>{config?.configured ? "已加密保存" : "未配置"}</dd>
            </div>
            <div>
              <dt>Webhook域名</dt>
              <dd>{config?.webhook_host || "—"}</dd>
            </div>
            <div>
              <dt>最近发送</dt>
              <dd>{formatTime(config?.last_delivery_at)}</dd>
            </div>
            <div>
              <dt>结果</dt>
              <dd>
                <StatusBadge
                  status={config?.last_delivery_status || "unknown"}
                />
              </dd>
            </div>
          </dl>
          <p className="empty-note">
            故障与恢复均连续2次确认；敏感配置不会回显。
          </p>
        </Panel>
      </div>
      {reauthAction && (
        <PasswordReauthModal
          busy={reauthBusy}
          error={reauthError}
          onCancel={() => {
            if (reauthBusy) return;
            setReauthAction(null);
            setPendingConfig(null);
            setReauthError("");
          }}
          onSubmit={(password) => void finishReauth(password)}
        />
      )}
    </section>
  );
}

function Resources({
  health,
  ocrHealth,
  faceHealth,
  refresh,
  setError,
  showNotice,
}: {
  health: Health | null;
  ocrHealth: Record<string, unknown> | null;
  faceHealth: Record<string, unknown> | null;
  refresh: () => Promise<void>;
  setError: (value: string) => void;
  showNotice: (value: string) => void;
}) {
  async function gpuAction(gpuId: number, action: string) {
    if (
      !window.confirm(`确认对 GPU${gpuId} 执行 ${action}？这会影响关联服务。`)
    )
      return;
    try {
      await apiRequest("control", `/v1/admin/gpus/${gpuId}/${action}`, {
        method: "POST",
      });
      showNotice(`GPU${gpuId} ${action} 已完成`);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "GPU操作失败");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="INFRASTRUCTURE"
        title="GPU 与服务"
        description="查看所有推理服务；GPU生命周期操作只作用于服务白名单。"
      />
      <div className="gpu-grid">
        {(health?.gpus || []).map((gpu) => (
          <Panel
            title={`GPU ${gpu.gpu_id}`}
            eyebrow={gpu.enabled ? "ENABLED" : "DISABLED"}
            key={gpu.gpu_id}
          >
            <div className="gpu-summary">
              <div className="gpu-orbit">
                <strong>{gpu.gpu_id}</strong>
                <span>GPU</span>
              </div>
              <div>
                <StatusBadge status={gpu.enabled ? "enabled" : "disabled"} />
                <p>
                  {
                    gpu.services.filter(
                      (service) => service.ActiveState === "active",
                    ).length
                  }
                  /{gpu.services.length} 服务运行
                </p>
              </div>
            </div>
            <div className="unit-list">
              {gpu.services.map((service) => (
                <div key={service.name}>
                  <i
                    className={classNames(
                      "signal",
                      service.ActiveState === "active" && "signal-live",
                    )}
                  />
                  <span>
                    <strong>{service.name}</strong>
                    <small>
                      {service.ActiveState} · {service.SubState}
                    </small>
                  </span>
                </div>
              ))}
            </div>
            <div className="danger-actions">
              <button onClick={() => void gpuAction(gpu.gpu_id, "enable")}>
                启用
              </button>
              <button onClick={() => void gpuAction(gpu.gpu_id, "drain")}>
                排空
              </button>
              <button
                className="danger-button"
                onClick={() => void gpuAction(gpu.gpu_id, "disable")}
              >
                禁用
              </button>
            </div>
          </Panel>
        ))}
      </div>
      <div className="two-column">
        <Panel title="OCR Gateway" eyebrow="SERVICE DETAIL">
          <ResultBox value={ocrHealth} empty="加载中…" />
        </Panel>
        <Panel title="Face Worker" eyebrow="SERVICE DETAIL">
          <ResultBox value={faceHealth} empty="加载中…" />
        </Panel>
      </div>
      <HealthMonitorPanel setError={setError} showNotice={showNotice} />
    </>
  );
}

function Models({
  health,
  voices,
  providers,
  reload,
  setError,
  showNotice,
}: {
  health: Health | null;
  voices: VoiceProfile[];
  providers: Array<Record<string, unknown>>;
  reload: () => Promise<void>;
  setError: (value: string) => void;
  showNotice: (value: string) => void;
}) {
  const [selected, setSelected] = useState("");
  const voice =
    voices.find((item) => item.voice_profile_id === selected) || voices[0];
  const [json, setJson] = useState("");
  const editorValue = json || (voice ? JSON.stringify(voice, null, 2) : "");
  async function save() {
    try {
      const profile = JSON.parse(editorValue) as VoiceProfile;
      await apiRequest(
        "control",
        `/v2/tts/voices/${encodeURIComponent(profile.voice_profile_id)}`,
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(profile),
        },
      );
      showNotice("音色档案已更新");
      await reload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
    }
  }
  const musetalk = health?.upstreams?.musetalk?.details || {};
  return (
    <>
      <PageHeading
        eyebrow="MODELS & VOICES"
        title="模型与音色"
        description="模型只读展示，音色档案变更会写入版本记录。"
      />
      <div className="two-column">
        <Panel title="MuseTalk 模型完整性" eyebrow="READ ONLY">
          <div className="model-health">
            <StatusBadge status={String(musetalk.status || "unknown")} />
            <dl>
              <div>
                <dt>GPU</dt>
                <dd>{String(musetalk.gpu ?? "0")}</dd>
              </div>
              <div>
                <dt>GFPGAN</dt>
                <dd>{musetalk.gfpgan_enabled ? "已启用" : "未启用"}</dd>
              </div>
              <div>
                <dt>缺失文件</dt>
                <dd>
                  {Array.isArray(musetalk.missing_artifacts)
                    ? musetalk.missing_artifacts.length
                    : "—"}
                </dd>
              </div>
            </dl>
            <p>模型路径和密钥不会显示在浏览器中。</p>
          </div>
        </Panel>
        <Panel title="TTS Provider" eyebrow="UPSTREAMS">
          <div className="provider-list compact">
            {providers.map((provider, index) => (
              <article key={index}>
                <strong>
                  {String(provider.name || provider.provider || index)}
                </strong>
                <StatusBadge status={String(provider.status || "configured")} />
              </article>
            ))}
          </div>
        </Panel>
      </div>
      <Panel title="音色档案" eyebrow="VOICE REGISTRY">
        <div className="voice-editor">
          <aside>
            {voices.map((item) => (
              <button
                className={
                  item.voice_profile_id === voice?.voice_profile_id
                    ? "is-active"
                    : ""
                }
                onClick={() => {
                  setSelected(item.voice_profile_id);
                  setJson(JSON.stringify(item, null, 2));
                }}
                key={item.voice_profile_id}
              >
                <strong>{item.display_name}</strong>
                <small>
                  {item.voice_profile_id} · v{item.version}
                </small>
              </button>
            ))}
          </aside>
          <div>
            <textarea
              rows={20}
              value={editorValue}
              onChange={(event) => setJson(event.target.value)}
              spellCheck={false}
            />
            <button
              className="primary-button"
              onClick={() => void save()}
              disabled={!voice}
            >
              校验并保存
            </button>
          </div>
        </div>
      </Panel>
    </>
  );
}

function Logs({
  jobs,
  setError,
}: {
  jobs: LipSyncJob[];
  setError: (value: string) => void;
}) {
  const [jobId, setJobId] = useState(jobs[0]?.job_id || "");
  const [stage, setStage] = useState("musetalk");
  const [log, setLog] = useState("");
  async function load() {
    if (!jobId) return;
    try {
      const result = await apiRequest<{ log: string }>(
        "control",
        `/v1/lipsync/jobs/${jobId}/logs?stage=${stage}&tail=300`,
      );
      setLog(result.log);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "日志加载失败");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="SAFE LOG TAIL"
        title="任务日志"
        description="只允许读取任务私有目录中的 MuseTalk/GFPGAN 日志，最多返回 500 行。"
      />
      <Panel title="日志查看器" eyebrow="JOB LOG">
        <div className="log-toolbar">
          <Select
            value={jobId}
            onChange={(event) => setJobId(event.target.value)}
          >
            <option value="">选择任务</option>
            {jobs.map((job) => (
              <option value={job.job_id} key={job.job_id}>
                {job.job_id.slice(0, 8)} · {job.state}
              </option>
            ))}
          </Select>
          <Select
            value={stage}
            onChange={(event) => setStage(event.target.value)}
          >
            <option value="musetalk">MuseTalk</option>
            <option value="gfpgan">GFPGAN</option>
          </Select>
          <button className="secondary-button" onClick={() => void load()}>
            读取最后 300 行
          </button>
        </div>
        <pre className="terminal-output">
          {log || "选择任务和阶段后读取日志。"}
        </pre>
      </Panel>
    </>
  );
}

function Audit({
  events,
  reload,
}: {
  events: AuditEvent[];
  reload: () => Promise<void>;
}) {
  return (
    <>
      <PageHeading
        eyebrow="ADMIN AUDIT"
        title="操作审计"
        description="记录管理操作的时间、对象和结果，不记录Token、密码和业务正文。"
        actions={
          <button className="secondary-button" onClick={() => void reload()}>
            刷新
          </button>
        }
      />
      <Panel title="最近 200 条记录" eyebrow="JSONL PERSISTENCE">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>操作者</th>
                <th>动作</th>
                <th>资源</th>
                <th>来源</th>
                <th>结果</th>
              </tr>
            </thead>
            <tbody>
              {events.length === 0 && (
                <tr>
                  <td colSpan={6} className="empty-cell">
                    暂无审计记录
                  </td>
                </tr>
              )}
              {events.map((event) => (
                <tr key={event.id}>
                  <td>{formatTime(event.timestamp)}</td>
                  <td>{event.username}</td>
                  <td className="mono">{event.action}</td>
                  <td className="mono wrap">{event.resource}</td>
                  <td>{event.address}</td>
                  <td>
                    <StatusBadge status={event.outcome} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </>
  );
}

function ImageCapabilities() {
  const [result,setResult]=useState<Record<string,unknown>|null>(null);
  const [busy,setBusy]=useState(false);
  async function submit(event:FormEvent<HTMLFormElement>){event.preventDefault();setBusy(true);const data=new FormData(event.currentTarget);try{const urls=String(data.get("urls")||"").split(/\r?\n/).map(v=>v.trim()).filter(Boolean);const response=await apiRequest<Record<string,unknown>>("control","/v1/image-generations/jobs",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({model:String(data.get("model")),channel:"grsai",prompt:String(data.get("prompt")),aspect_ratio:String(data.get("aspect_ratio")),image_size:String(data.get("image_size")||"1K"),reference_image_urls:urls})});setResult(response)}catch(reason){setResult({error:uiError(reason)})}finally{setBusy(false)}}
  return (
    <>
      <PageHeading
        eyebrow="IMAGE CAPABILITIES"
        title="图像能力"
        description="通过GPT Image 2生成图片，支持纯文本和参考图片。"
      />
      <div className="two-column wide-left"><Panel title="GRSAI 图像生成" eyebrow="ASYNC IMAGE GENERATION"><form className="stack-form" onSubmit={submit}><label>模型<Select name="model" defaultValue="gpt-image-2"><option value="gpt-image-2">GPT Image 2</option><option value="gpt-image-2.5">GPT Image 2.5</option><option value="gpt-image-2.5-sunburst">GPT Image 2.5 Sunburst</option><option value="gpt-image-2.5-flare">GPT Image 2.5 Flare</option><option value="nano-banana-2">Nano Banana 2</option></Select></label><label>提示词<textarea name="prompt" rows={5} required placeholder="描述要生成的图片"/></label><label>参考图片URL（每行一个，可选）<textarea name="urls" rows={4} placeholder="https://storage.example.com/reference.png"/></label><label>画面比例<Select name="aspect_ratio" defaultValue="1:1"><option value="1:1">1:1</option><option value="2:3">2:3</option><option value="3:2">3:2</option><option value="3:4">3:4</option><option value="4:3">4:3</option><option value="9:16">9:16</option><option value="16:9">16:9</option></Select></label><label>图片尺寸<Select name="image_size" defaultValue="1K"><option value="1K">1K</option><option value="2K">2K</option><option value="4K">4K</option></Select></label><button className="primary-button" disabled={busy}>{busy?"提交中…":"提交异步任务"}</button></form></Panel><Panel title="提交结果" eyebrow="RESULT"><ResultBox value={result} empty="提交后显示任务ID和查询地址。"/></Panel></div>
    </>
  );
}

function OssStorage({setError,showNotice}:{setError:(value:string)=>void;showNotice:(value:string)=>void}) {
  const [status,setStatus]=useState<Record<string,unknown>|null>(null);
  const [results,setResults]=useState<Array<{filename:string;url:string;key:string;bytes:number;content_type:string}>>([]);
  const [busy,setBusy]=useState(false);
  useEffect(()=>{void apiRequest<Record<string,unknown>>("control","/internal/admin/storage/status").then(setStatus).catch(reason=>setError(uiError(reason)))},[setError]);
  async function submit(event:FormEvent<HTMLFormElement>){
    event.preventDefault();setBusy(true);setError("");
    const data=new FormData(event.currentTarget);const files=data.getAll("files").filter((value):value is File=>value instanceof File&&value.size>0);const prefix=String(data.get("prefix")||"");
    try{
      const uploaded=[];
      for(const file of files){const body=new FormData();body.append("file",file);if(prefix)body.append("prefix",prefix);uploaded.push(await apiRequest<{filename:string;url:string;key:string;bytes:number;content_type:string}>("control","/internal/admin/storage/upload",{method:"POST",body}))}
      setResults(uploaded);showNotice(`已上传 ${uploaded.length} 个文件到OSS`);
    }catch(reason){setError(uiError(reason))}finally{setBusy(false)}
  }
  return <>
    <PageHeading eyebrow="OBJECT STORAGE" title="OSS 对象存储" description="上传图片、音频、视频和其他业务素材，返回可直接用于AI接口的公网URL。" />
    <div className="metric-grid compact">
      <article className="metric-card accent"><span>配置状态</span><strong>{status?.configured?"已连接":"未配置"}</strong></article>
      <article className="metric-card"><span>存储桶</span><strong>{String(status?.bucket||"—")}</strong></article>
      <article className="metric-card"><span>默认目录</span><strong>{String(status?.default_prefix||"—")}</strong></article>
      <article className="metric-card"><span>单文件上限</span><strong>512 MiB</strong></article>
    </div>
    <div className="two-column wide-left">
      <Panel title="上传素材" eyebrow="UPLOAD"><form className="stack-form" onSubmit={submit}>
        <label>OSS目录前缀<input name="prefix" defaultValue={String(status?.default_prefix||"ai-centre/uploads")} placeholder="ai-centre/uploads"/></label>
        <label>选择文件<input name="files" type="file" multiple required/></label>
        <p className="helper-text">支持批量上传；文件不会复制到任务日志，凭证不会传到浏览器。</p>
        <button className="primary-button" disabled={busy||status?.configured===false}>{busy?"上传中…":"上传到OSS"}</button>
      </form></Panel>
      <Panel title="上传结果" eyebrow="PUBLIC URL">{results.length===0?<div className="empty-state">上传完成后在这里显示公网URL。</div>:<div className="stack-list">{results.map(item=><article key={item.key}><div><strong>{item.filename}</strong><small>{(item.bytes/1024/1024).toFixed(2)} MiB · {item.content_type}</small></div><a className="text-button" href={item.url} target="_blank" rel="noreferrer">打开文件</a><code className="mono wrap">{item.url}</code></article>)}</div>}</Panel>
    </div>
  </>;
}

function ProjectStatus() {
  const services = [
    "Control Plane",
    "Faster-Whisper",
    "VoxCPM2 / TTS",
    "OCR Gateway",
    "Face Mosaic",
    "MuseTalk 1.5 + GFPGAN",
    "Bandit v2 Audio Separation",
    "Caddy HTTPS",
    "Redis / Celery",
    "SQLite Observability",
  ];
  return (
    <>
      <PageHeading
        eyebrow="PROJECT STATUS"
        title="项目开发情况"
        description="生产部署、开发能力和测试结论的统一交接视图。"
      />
      <div className="deployment-strip">
        <div>
          <span>生产服务器</span>
          <strong>121.15.184.231:2222</strong>
        </div>
        <div>
          <span>部署目录</span>
          <strong>/home/donxu/ai-centre</strong>
        </div>
        <div>
          <span>公网入口</span>
          <strong>https://aicentre2.sligenai.cn:8443</strong>
        </div>
      </div>
      <Panel title="已部署能力" eyebrow="PRODUCTION SERVICES">
        <div className="capability-grid">
          {services.map((service, index) => (
            <article key={service}>
              <span>{String(index + 1).padStart(2, "0")}</span>
              <strong>{service}</strong>
              <StatusBadge status="已部署" />
            </article>
          ))}
        </div>
      </Panel>
      <div className="two-column">
        <Panel title="测试情况" eyebrow="VERIFICATION">
          <ul className="check-list">
            <li>服务器控制面测试：95项通过</li>
            <li>后台测试、Lint、生产构建：通过</li>
            <li>TTS、ASR、OCR、Face、Scene真实联调：通过</li>
            <li>内部观测接口公网隔离：固定404</li>
          </ul>
        </Panel>
        <Panel title="当前约束" eyebrow="BOUNDARIES">
          <ul className="check-list warning">
            <li>GPU0 唇形任务并发为 1</li>
            <li>字幕检测后端8097当前未运行</li>
            <li>正式价格未配置，默认仅统计用量</li>
            <li>任务产物暂不自动清理</li>
          </ul>
        </Panel>
      </div>
    </>
  );
}

function Bugs() {
  const items = [
    {
      id: "BUG-101",
      level: "P1",
      title: "动漫素材开启 GFPGAN 后身份漂移",
      detail: "人脸后置修复可能重绘动漫五官，因此生产默认关闭。",
      owner: "MuseTalk",
    },
    {
      id: "BUG-102",
      level: "P2",
      title: "本地全仓测试环境不统一",
      detail: "本机缺少部分OCR/Celery依赖，生产服务器95项控制面测试正常。",
      owner: "工程效率",
    },
    {
      id: "BUG-103",
      level: "P1",
      title: "字幕检测后端当前未运行",
      detail:
        "127.0.0.1:8097 无监听；后台请求会返回503并正确进入失败任务与错误排行。",
      owner: "Subtitle",
    },
  ];
  return (
    <>
      <PageHeading
        eyebrow="BUG REGISTER"
        title="遗留 Bug"
        description="缺陷与下一阶段建设分开维护，避免优先级混淆。"
      />
      <div className="bug-list">
        {items.map((item) => (
          <article key={item.id}>
            <div className={`priority priority-${item.level.toLowerCase()}`}>
              {item.level}
            </div>
            <span className="mono">{item.id}</span>
            <div>
              <h2>{item.title}</h2>
              <p>{item.detail}</p>
            </div>
            <small>{item.owner}</small>
          </article>
        ))}
      </div>
    </>
  );
}

function Roadmap() {
  const phases = [
    {
      phase: "01",
      title: "统一观测与任务中心",
      state: "已完成",
      items: [
        "七类服务调用和任务记录",
        "请求响应脱敏加密与二次认证",
        "7天/30天仪表盘和版本化计费",
      ],
    },
    {
      phase: "02",
      title: "告警与预算治理",
      state: "下一阶段",
      items: [
        "失败率、延迟和积压阈值告警",
        "GPU成本与月度预算预警",
        "服务SLA与自动日报",
      ],
    },
    {
      phase: "03",
      title: "素材库与对象存储",
      state: "规划",
      items: ["素材去重和元数据", "预览、标签与项目归档", "可配置产物保留策略"],
    },
    {
      phase: "04",
      title: "客户化计费与权限",
      state: "规划",
      items: [
        "客户API Key、配额和限流",
        "管理员/操作员/审计员RBAC",
        "客户账单、对账和发票接口",
      ],
    },
  ];
  return (
    <>
      <PageHeading
        eyebrow="NEXT STAGE"
        title="下一阶段"
        description="建设路线不与 Bug 混写，每个阶段都有清晰的生产目标。"
      />
      <div className="roadmap-list">
        {phases.map((phase) => (
          <article key={phase.phase}>
            <span>{phase.phase}</span>
            <div>
              <small>{phase.state}</small>
              <h2>{phase.title}</h2>
              <ul>
                {phase.items.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          </article>
        ))}
      </div>
    </>
  );
}

function Settings({ health }: { health: Health | null }) {
  return (
    <>
      <PageHeading
        eyebrow="SYSTEM SETTINGS"
        title="API 与设置"
        description="这里只显示可公开的运行信息；生产密钥、密码哈希和模型路径保持隐藏。"
      />
      <div className="two-column">
        <Panel title="连接信息" eyebrow="READ ONLY">
          <dl className="settings-list">
            <div>
              <dt>后台入口</dt>
              <dd>/admin</dd>
            </div>
            <div>
              <dt>控制面</dt>
              <dd>127.0.0.1:8320</dd>
            </div>
            <div>
              <dt>MuseTalk</dt>
              <dd>127.0.0.1:9011</dd>
            </div>
            <div>
              <dt>后台服务</dt>
              <dd>127.0.0.1:8340</dd>
            </div>
            <div>
              <dt>状态</dt>
              <dd>
                <StatusBadge status={health?.status || "unknown"} />
              </dd>
            </div>
          </dl>
        </Panel>
        <Panel title="安全策略" eyebrow="ENFORCED">
          <ul className="check-list">
            <li>8小时HttpOnly安全会话</li>
            <li>服务Token仅服务端注入</li>
            <li>固定内部API白名单</li>
            <li>变更操作写入审计日志</li>
            <li>GPU操作二次确认</li>
          </ul>
        </Panel>
      </div>
      <Panel title="API 范围" eyebrow="ALLOWLIST">
        <div className="api-list">
          <code>POST /v1/lipsync/jobs</code>
          <code>POST /v1/asr/transcriptions</code>
          <code>POST /v2/tts/speech</code>
          <code>POST /v1/ocr/batch</code>
          <code>POST /v1/face-mosaic/jobs</code>
          <code>GET /v1/admin/gpus</code>
        </div>
      </Panel>
    </>
  );
}

function ApiKeyManagement({
  setError,
  showNotice,
}: {
  setError: (value: string) => void;
  showNotice: (value: string) => void;
}) {
  const [items, setItems] = useState<ManagedApiKey[]>([]);
  const [createdKey, setCreatedKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<ManagedApiKey | null>(null);
  const [editBusy, setEditBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      setItems(
        (
          await apiRequest<{ items: ManagedApiKey[] }>(
            "control",
            "/internal/admin/api-keys",
          )
        ).items,
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "API Key 加载失败");
    }
  }, [setError]);
  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setCreatedKey("");
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const expires = String(data.get("expires_at") || "");
      const quota = String(data.get("quota") || "");
      const result = await apiRequest<ManagedApiKey>(
        "control",
        "/internal/admin/api-keys",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            name: data.get("name"),
            quota: quota ? Number(quota) : null,
            expires_at: expires ? new Date(expires).toISOString() : null,
          }),
        },
      );
      setCreatedKey(result.api_key || "");
      form.reset();
      showNotice("API Key 已创建，请立即复制保存");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "API Key 创建失败");
    } finally {
      setBusy(false);
    }
  }
  async function toggle(item: ManagedApiKey) {
    try {
      await apiRequest("control", `/internal/admin/api-keys/${item.id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ enabled: !item.enabled }),
      });
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "状态修改失败");
    }
  }
  async function saveEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editing) return;
    setEditBusy(true);
    const data = new FormData(event.currentTarget);
    const quota = String(data.get("quota") || "");
    const expires = String(data.get("expires_at") || "");
    try {
      await apiRequest("control", `/internal/admin/api-keys/${editing.id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          name: String(data.get("name") || "").trim(),
          quota: quota ? Number(quota) : null,
          expires_at: expires ? new Date(expires).toISOString() : null,
        }),
      });
      setEditing(null);
      showNotice("API Key 信息已更新");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "API Key 编辑失败");
    } finally {
      setEditBusy(false);
    }
  }
  async function remove(item: ManagedApiKey) {
    if (
      !window.confirm(
        `确认永久删除 API Key“${item.name}”？删除后调用方会立即失效。`,
      )
    )
      return;
    try {
      await apiRequest("control", `/internal/admin/api-keys/${item.id}`, {
        method: "DELETE",
      });
      showNotice("API Key 已删除");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除失败");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="ACCESS CONTROL"
        title="API Key 管理"
        description="为不同调用方创建独立密钥，设置总请求额度与过期时间。密钥明文只展示一次。"
        actions={
          <button className="secondary-button" onClick={() => void load()}>
            刷新
          </button>
        }
      />
      <div className="api-key-create-grid">
        <Panel title="新建 API Key" eyebrow="CREATE KEY">
          <form className="stack-form" onSubmit={create}>
            <label>
              名称
              <input
                name="name"
                required
                maxLength={128}
                placeholder="例如：视频业务生产环境"
              />
            </label>
            <div className="field-row">
              <label>
                请求额度（可选）
                <input
                  name="quota"
                  type="number"
                  min="1"
                  step="1"
                  placeholder="留空表示不限次数"
                />
                <small>每次鉴权请求消耗1次。</small>
              </label>
              <label>
                过期时间（可选）
                <input name="expires_at" type="datetime-local" />
                <small>留空表示永不过期。</small>
              </label>
            </div>
            <button className="primary-button" disabled={busy}>
              {busy ? "创建中…" : "创建 API Key"}
            </button>
          </form>
        </Panel>
        <Panel title="一次性密钥" eyebrow="COPY NOW">
          {createdKey ? (
            <>
              <div className="note-card">
                <b>只会显示这一次</b>
                <pre className="mono">{createdKey}</pre>
              </div>
              <button
                className="secondary-button"
                onClick={() => void navigator.clipboard.writeText(createdKey)}
              >
                复制 API Key
              </button>
              <p className="empty-note">
                请求头格式：Authorization: Bearer {createdKey.slice(0, 12)}…
              </p>
            </>
          ) : (
            <p className="empty-note">
              创建后，完整 API Key
              会在这里显示一次。数据库只保存哈希，之后无法找回明文。
            </p>
          )}
        </Panel>
      </div>
      <Panel title="已创建的 API Key" eyebrow="KEYS">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>名称 / 前缀</th>
                <th>状态</th>
                <th>额度</th>
                <th>过期时间</th>
                <th>最近使用</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.length === 0 && (
                <tr>
                  <td colSpan={6} className="empty-cell">
                    暂无独立 API Key
                  </td>
                </tr>
              )}
              {items.map((item) => (
                <tr key={item.id}>
                  <td>
                    <strong>{item.name}</strong>
                    <small className="mono">{item.prefix}…</small>
                  </td>
                  <td>
                    <StatusBadge
                      status={item.enabled ? "enabled" : "disabled"}
                    />
                  </td>
                  <td>
                    {item.quota === null
                      ? "不限"
                      : `${item.used} / ${item.quota}`}
                    <small>
                      {item.remaining === null ? "" : `剩余 ${item.remaining}`}
                    </small>
                  </td>
                  <td>
                    {item.expires_at ? formatTime(item.expires_at) : "永不过期"}
                  </td>
                  <td>{formatTime(item.last_used_at)}</td>
                  <td>
                    <div className="row-actions">
                      <button onClick={() => setEditing(item)}>编辑</button>
                      <button onClick={() => void toggle(item)}>
                        {item.enabled ? "停用" : "启用"}
                      </button>
                      <button
                        className="danger-link"
                        onClick={() => void remove(item)}
                      >
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      {editing && (
        <Modal
          title={`编辑 API Key · ${editing.name}`}
          onClose={() => !editBusy && setEditing(null)}
          footer={<><button className="secondary-button" type="button" disabled={editBusy} onClick={() => setEditing(null)}>取消</button><button className="primary-button" type="submit" form="edit-api-key-form" disabled={editBusy}>{editBusy?"保存中…":"保存修改"}</button></>}
        >
          <form id="edit-api-key-form" className="stack-form" onSubmit={saveEdit}>
            <label>名称<input name="name" required maxLength={128} defaultValue={editing.name}/></label>
            <label>总请求额度（可选）<input name="quota" type="number" min="1" max="1000000000" step="1" defaultValue={editing.quota??""} placeholder="留空表示不限次数"/><small>已使用 {editing.used} 次；降低到已使用次数以下会使该Key立即无剩余额度。</small></label>
            <label>过期时间（可选）<input name="expires_at" type="datetime-local" defaultValue={editing.expires_at?new Date(new Date(editing.expires_at).getTime()-new Date(editing.expires_at).getTimezoneOffset()*60000).toISOString().slice(0,16):""}/><small>留空表示永不过期，只能设置未来时间。</small></label>
            <div className="note-card"><b>安全说明</b><p>只修改名称、额度和有效期，不会更换API Key明文或前缀，调用方无需修改配置。</p></div>
          </form>
        </Modal>
      )}
    </>
  );
}

function ResultBox({ value, empty }: { value: unknown; empty: string }) {
  return value ? (
    <pre className="result-box">{JSON.stringify(value, null, 2)}</pre>
  ) : (
    <p className="empty-note">{empty}</p>
  );
}

export type BackendName = "control" | "face" | "ocr" | "subtitle";

const POLICIES: Record<BackendName, Array<{ methods: string[]; pattern: RegExp }>> = {
  control: [
    { methods: ["GET"], pattern: /^\/health$/ },
    { methods: ["POST"], pattern: /^\/v1\/asr\/transcriptions\/upload$/ },
    { methods: ["GET", "POST"], pattern: /^\/v1\/lipsync\/jobs(?:\/upload|\/[0-9a-f-]+(?:\/(?:video|logs|cancel))?)?$/ },
    { methods: ["GET", "POST"], pattern: /^\/v1\/video-scenes\/jobs(?:\/wait|\/[0-9a-f-]+(?:\/cancel)?)?$/ },
    { methods: ["GET", "POST"], pattern: /^\/v1\/video-depth\/jobs(?:\/wait|\/[0-9a-f-]+(?:\/cancel)?)?$/ },
    { methods: ["GET", "POST"], pattern: /^\/v1\/audio-separation\/jobs(?:\/wait|\/[0-9a-f-]+(?:\/cancel)?)?$/ },
    { methods: ["GET", "POST"], pattern: /^\/v1\/video-upscale\/jobs(?:\/wait|\/[0-9a-f-]+(?:\/cancel)?)?$/ },
    { methods: ["GET", "POST"], pattern: /^\/v1\/video-generations\/minimax-h3\/jobs(?:\/[0-9a-f-]+(?:\/(?:cancel|queue-position))?)?$/ },
    { methods: ["GET", "POST"], pattern: /^\/v1\/video-generations\/jobs(?:\/[0-9a-f-]+(?:\/cancel)?)?$/ },
    { methods: ["GET", "POST"], pattern: /^\/v1\/image-generations\/jobs(?:\/[0-9a-f-]+(?:\/cancel)?)?$/ },
    { methods: ["GET", "POST"], pattern: /^\/v1\/audio-generations\/jobs(?:\/[0-9a-f-]+(?:\/cancel)?)?$/ },
    { methods: ["GET", "POST"], pattern: /^\/internal\/admin\/storage\/(?:status|upload)$/ },
    { methods: ["GET", "POST"], pattern: /^\/internal\/admin\/ai-capabilities\/(?:channels|models|jobs)(?:\/[0-9a-f-]+(?:\/test)?)?$/ },
    { methods: ["PATCH", "DELETE"], pattern: /^\/internal\/admin\/ai-capabilities\/channels\/[0-9a-f-]+$/ },
    { methods: ["GET"], pattern: /^\/v1\/video-generations\/minimax-h3\/workers\/status$/ },
    { methods: ["GET"], pattern: /^\/internal\/admin\/h3\/jobs(?:\/[0-9a-f-]+)?$/ },
    { methods: ["GET", "POST"], pattern: /^\/internal\/admin\/h3\/workers$/ },
    { methods: ["PATCH", "DELETE"], pattern: /^\/internal\/admin\/h3\/workers\/[0-9a-f-]+$/ },
    { methods: ["POST"], pattern: /^\/internal\/admin\/h3\/workers\/[0-9a-f-]+\/(?:test|drain)$/ },
    { methods: ["GET"], pattern: /^\/internal\/admin\/h3\/pool$/ },
    { methods: ["GET"], pattern: /^\/internal\/admin\/h3\/pool\/deployments$/ },
    { methods: ["PUT"], pattern: /^\/internal\/admin\/h3\/pool\/config$/ },
    { methods: ["POST"], pattern: /^\/internal\/admin\/h3\/pool\/workers$/ },
    { methods: ["DELETE"], pattern: /^\/internal\/admin\/h3\/pool\/workers\/[0-9a-f-]+$/ },
    { methods: ["GET", "POST"], pattern: /^\/internal\/admin\/api-keys$/ },
    { methods: ["PATCH", "DELETE"], pattern: /^\/internal\/admin\/api-keys\/[0-9a-f-]+$/ },
    { methods: ["GET", "POST"], pattern: /^\/v2\/tts\/(?:speech(?:\/(?:upload|stream))?|jobs|providers|voices|quality)(?:\/[A-Za-z0-9_-]+(?:\/audio)?)?$/ },
    { methods: ["PUT"], pattern: /^\/v2\/tts\/voices\/[A-Za-z0-9_-]+$/ },
    { methods: ["POST"], pattern: /^\/internal\/admin\/subtitle\/detect$/ },
    { methods: ["POST"], pattern: /^\/internal\/admin\/ocr\/batch$/ },
    { methods: ["GET", "POST"], pattern: /^\/internal\/admin\/observability\/(?:health|overview|analytics\/(?:summary|timeseries|stages|h3-duration)|calls(?:\/[0-9a-f-]+)?|tasks(?:\/[A-Za-z0-9_-]+)?|pricing-rules|export\.csv|maintenance\/cleanup)$/ },
    { methods: ["GET"], pattern: /^\/internal\/admin\/health-monitor\/(?:targets|status|history|incidents|config)$/ },
    { methods: ["POST"], pattern: /^\/internal\/admin\/health-monitor\/run-check$/ },
    { methods: ["GET"], pattern: /^\/internal\/admin\/concurrency$/ },
    { methods: ["GET"], pattern: /^\/v1\/admin\/gpus$/ },
    { methods: ["POST"], pattern: /^\/v1\/admin\/gpus\/[01]\/(?:drain|disable|enable)$/ },
  ],
  face: [
    { methods: ["GET"], pattern: /^\/health$/ },
    { methods: ["GET", "POST"], pattern: /^\/v1\/face-mosaic\/jobs(?:\/[0-9a-f-]+(?:\/cancel)?)?$/ },
  ],
  ocr: [
    { methods: ["GET"], pattern: /^\/(?:health|metrics)$/ },
    { methods: ["POST"], pattern: /^\/v1\/ocr\/batch$/ },
  ],
  subtitle: [
    { methods: ["GET"], pattern: /^\/health$/ },
    { methods: ["POST"], pattern: /^\/v1\/subtitle-events\/detect$/ },
  ],
};

export function isAllowedProxyRequest(backend: string, method: string, path: string): boolean {
  const policies = POLICIES[backend as BackendName];
  return Boolean(
    policies?.some(
      (policy) => policy.methods.includes(method.toUpperCase()) && policy.pattern.test(path),
    ),
  );
}

export function backendUrl(backend: BackendName): string {
  const values: Record<BackendName, string> = {
    control: process.env.AI_CENTRE_CONTROL_URL || "http://127.0.0.1:8320",
    face: process.env.AI_CENTRE_FACE_URL || "http://127.0.0.1:8310",
    ocr: process.env.AI_CENTRE_OCR_URL || "http://127.0.0.1:8096",
    subtitle: process.env.AI_CENTRE_SUBTITLE_URL || "http://127.0.0.1:8097",
  };
  return values[backend];
}

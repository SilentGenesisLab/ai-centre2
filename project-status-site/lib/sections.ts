export const SECTIONS = [
  { id: "overview", label: "数据分析" }, { id: "calls", label: "任务日志" },
  { id: "billing", label: "计费中心" }, { id: "api-keys", label: "API Key 管理" },
  { id: "asr", label: "语音识别" }, { id: "ocr", label: "OCR 与字幕" },
  { id: "tts", label: "语音合成" }, { id: "separation", label: "音频分离" },
  { id: "lipsync", label: "唇形驱动" }, { id: "face", label: "人脸处理" },
  { id: "scene", label: "视频切片" }, { id: "depth", label: "视频深度推理" },
  { id: "upscale", label: "视频超分" },
  { id: "h3", label: "MiniMax H3 生成" }, { id: "image", label: "图像能力" },
  { id: "resources", label: "GPU 与服务" }, { id: "models", label: "模型与音色" },
  { id: "audit", label: "操作审计" }, { id: "project", label: "项目状态" },
  { id: "bugs", label: "遗留 Bug" }, { id: "roadmap", label: "下一阶段" },
  { id: "settings", label: "系统设置" }, { id: "logs", label: "任务日志（旧入口）" },
] as const;
export type SectionId = (typeof SECTIONS)[number]["id"];
export type NavigationItem = { id: string; label: string; icon: string; href?: SectionId; children?: NavigationItem[]; disabled?: boolean };

export const NAVIGATION: NavigationItem[] = [
  { id: "overview", label: "数据分析", icon: "dashboard", href: "overview" },
  { id: "calls", label: "任务日志", icon: "logs", href: "calls" },
  { id: "billing", label: "计费中心", icon: "billing", href: "billing" },
  { id: "api-keys", label: "API Key 管理", icon: "key", href: "api-keys" },
  { id: "production", label: "生产任务", icon: "production", children: [
    { id: "text", label: "文本", icon: "text", children: [
      { id: "asr", label: "语音识别", icon: "asr", href: "asr" },
      { id: "ocr", label: "OCR 与字幕", icon: "ocr", href: "ocr" },
    ] },
    { id: "audio", label: "音频", icon: "audio", children: [
      { id: "tts", label: "语音合成", icon: "tts", href: "tts" },
      { id: "separation", label: "音频分离", icon: "separation", href: "separation" },
    ] },
    { id: "video", label: "视频", icon: "video", children: [
      { id: "lipsync", label: "唇形驱动", icon: "lipsync", href: "lipsync" },
      { id: "face", label: "人脸处理", icon: "face", href: "face" },
      { id: "scene", label: "视频切片", icon: "scene", href: "scene" },
      { id: "depth", label: "视频深度推理", icon: "depth", href: "depth" },
      { id: "upscale", label: "视频超分", icon: "depth", href: "upscale" },
      { id: "h3", label: "MiniMax H3 生成", icon: "h3", href: "h3" },
    ] },
    { id: "image", label: "图像", icon: "image", href: "image" },
  ] },
  { id: "resources", label: "GPU 与服务", icon: "gpu", href: "resources" },
  { id: "models", label: "模型与音色", icon: "models", href: "models" },
  { id: "audit", label: "操作审计", icon: "audit", href: "audit" },
  { id: "projects", label: "项目管理", icon: "project", children: [
    { id: "project", label: "项目状态", icon: "status", href: "project" },
    { id: "bugs", label: "遗留 Bug", icon: "bug", href: "bugs" },
    { id: "roadmap", label: "下一阶段", icon: "roadmap", href: "roadmap" },
  ] },
  { id: "settings", label: "系统设置", icon: "settings", href: "settings" },
];

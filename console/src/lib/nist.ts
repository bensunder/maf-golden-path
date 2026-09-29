// NIST references each running control supports, so an assessor can start from evidence instead of a blank page.
// The statuses come from the agent itself (its middleware stack, settings and latest quality gate); the mapping is
// ours, for review. It is not a certification or an assessment.
import type { Control, ControlStatus } from "@/lib/api";

export type FrameworkId = "ai_rmf" | "ai_600_1" | "csf_2" | "sp_800_53";

export interface Framework {
  id: FrameworkId;
  title: string;
  short: string;
  url: string;
}

export const FRAMEWORKS: Framework[] = [
  { id: "ai_rmf", title: "NIST AI Risk Management Framework 1.0 (AI 100-1)", short: "AI RMF", url: "https://doi.org/10.6028/NIST.AI.100-1" },
  { id: "ai_600_1", title: "NIST AI 600-1, Generative AI Profile", short: "GenAI Profile", url: "https://doi.org/10.6028/NIST.AI.600-1" },
  { id: "csf_2", title: "NIST Cybersecurity Framework 2.0", short: "CSF 2.0", url: "https://doi.org/10.6028/NIST.CSWP.29" },
  { id: "sp_800_53", title: "NIST SP 800-53 Rev. 5", short: "SP 800-53", url: "https://doi.org/10.6028/NIST.SP.800-53r5" },
];

export const REFERENCES: Record<FrameworkId, Record<string, string>> = {
  ai_rmf: {
    "MAP 3.5": "Processes for human oversight are defined, assessed, and documented",
    "MEASURE 2.1": "Test sets, metrics, and details about the tools used during TEVV are documented",
    "MEASURE 2.3": "Performance or assurance criteria are measured and demonstrated for conditions like deployment",
    "MEASURE 2.5": "The AI system is demonstrated to be valid and reliable",
    "MEASURE 2.7": "AI system security and resilience are evaluated and documented",
    "MEASURE 2.8": "Risks associated with transparency and accountability are examined and documented",
    "MEASURE 2.10": "Privacy risk of the AI system is examined and documented",
    "MANAGE 2.4": "Mechanisms are in place to supersede, disengage, or deactivate AI systems",
    "MANAGE 4.1": "Post-deployment AI system monitoring plans are implemented",
  },
  ai_600_1: {
    Confabulation: "Confidently stated but erroneous or false content",
    "Data Privacy": "Leakage, unauthorized use or disclosure of personal data",
    "Human-AI Configuration": "Arrangements between people and AI systems that lead to over-reliance, misalignment or harm",
    "Information Integrity": "Content that doesn't distinguish fact from opinion or fiction",
    "Information Security": "Attacks on GAI systems such as prompt injection, and lowered barriers to offensive cyber operations",
    "Value Chain and Component Integration": "Non-transparent or untraceable integration of third-party components, data and services",
  },
  csf_2: {
    "PR.AA-01": "Identities and credentials for authorized users, services, and hardware are managed",
    "PR.AA-03": "Users, services, and hardware are authenticated",
    "PR.AA-05": "Access permissions and authorizations are managed and enforced, with least privilege and separation of duties",
    "PR.DS-01": "The confidentiality, integrity, and availability of data-at-rest are protected",
    "PR.DS-02": "The confidentiality, integrity, and availability of data-in-transit are protected",
    "PR.DS-10": "The confidentiality, integrity, and availability of data-in-use are protected",
    "PR.PS-01": "Configuration management practices are established and applied",
    "PR.PS-04": "Log records are generated and made available for continuous monitoring",
    "PR.PS-06": "Secure software development practices are integrated and their performance is monitored",
    "PR.IR-04": "Adequate resource capacity to ensure availability is maintained",
    "DE.CM-09": "Hardware, software, runtime environments and their data are monitored to find potentially adverse events",
  },
  sp_800_53: {
    "AC-3": "Access Enforcement",
    "AC-3(2)": "Access Enforcement: Dual Authorization",
    "AC-4": "Information Flow Enforcement",
    "AC-5": "Separation of Duties",
    "AC-6": "Least Privilege",
    "AU-2": "Event Logging",
    "AU-3": "Content of Audit Records",
    "AU-3(3)": "Content of Audit Records: Limit Personally Identifiable Information Elements",
    "AU-12": "Audit Record Generation",
    "CM-7": "Least Functionality",
    "IA-2": "Identification and Authentication (Organizational Users)",
    "IA-9": "Service Identification and Authentication",
    "SA-11": "Developer Testing and Evaluation",
    "SC-4": "Information in Shared System Resources",
    "SC-5": "Denial-of-Service Protection",
    "SC-6": "Resource Availability",
    "SC-8": "Transmission Confidentiality and Integrity",
    "SC-23": "Session Authenticity",
    "SI-10": "Information Input Validation",
    "SI-15": "Information Output Filtering",
    "SI-19": "De-identification",
  },
};

type Mapping = Partial<Record<FrameworkId, string[]>>;

/** Control id (from the agent's security posture, plus the quality gate) → the references it supports. */
export const MAPPING: Record<string, Mapping> = {
  entra_auth: { ai_rmf: ["MEASURE 2.7"], ai_600_1: ["Information Security"], csf_2: ["PR.AA-01", "PR.AA-03"], sp_800_53: ["IA-2"] },
  platform_signing: { ai_rmf: ["MEASURE 2.7"], ai_600_1: ["Information Security"], csf_2: ["PR.AA-03", "PR.DS-02"], sp_800_53: ["IA-9", "SC-8", "SC-23"] },
  agent_delegation: {
    ai_rmf: ["MAP 3.5", "MEASURE 2.8"],
    ai_600_1: ["Human-AI Configuration", "Value Chain and Component Integration"],
    csf_2: ["PR.AA-05"],
    sp_800_53: ["AC-3", "AC-4", "AC-6"],
  },
  prompt_injection: { ai_rmf: ["MEASURE 2.7"], ai_600_1: ["Information Security"], csf_2: ["DE.CM-09"], sp_800_53: ["SI-10"] },
  tool_output: {
    ai_rmf: ["MEASURE 2.7"],
    ai_600_1: ["Information Security", "Information Integrity", "Value Chain and Component Integration"],
    csf_2: ["DE.CM-09"],
    sp_800_53: ["SI-10", "SI-15"],
  },
  pii: { ai_rmf: ["MEASURE 2.10"], ai_600_1: ["Data Privacy"], csf_2: ["PR.DS-10"], sp_800_53: ["SI-19"] },
  tool_policy: { ai_rmf: ["MANAGE 2.4"], ai_600_1: ["Information Security"], csf_2: ["PR.AA-05", "PR.PS-01"], sp_800_53: ["AC-6", "CM-7"] },
  human_approval: { ai_rmf: ["MAP 3.5", "MANAGE 2.4"], ai_600_1: ["Human-AI Configuration"], csf_2: ["PR.AA-05"], sp_800_53: ["AC-3(2)", "AC-5"] },
  session_isolation: { ai_rmf: ["MEASURE 2.10"], ai_600_1: ["Data Privacy"], csf_2: ["PR.AA-05", "PR.DS-10"], sp_800_53: ["AC-3", "SC-4"] },
  token_budget: { ai_rmf: ["MEASURE 2.7"], ai_600_1: ["Information Security"], csf_2: ["PR.IR-04"], sp_800_53: ["SC-5", "SC-6"] },
  audit: { ai_rmf: ["MEASURE 2.8", "MANAGE 4.1"], csf_2: ["PR.PS-04", "DE.CM-09"], sp_800_53: ["AU-2", "AU-3", "AU-12"] },
  content_capture: { ai_rmf: ["MEASURE 2.10"], ai_600_1: ["Data Privacy"], csf_2: ["PR.DS-01"], sp_800_53: ["AU-3(3)"] },
  quality_gate: {
    ai_rmf: ["MEASURE 2.1", "MEASURE 2.3", "MEASURE 2.5"],
    ai_600_1: ["Confabulation", "Information Integrity"],
    csf_2: ["PR.PS-06"],
    sp_800_53: ["SA-11"],
  },
};

export interface GateStatus {
  passed: boolean;
  live: boolean;
  pass_rate: number;
}

/** The latest quality gate as a control, so it counts as evidence like the runtime controls. */
export function gateControl(report: GateStatus | null | undefined): Control {
  if (!report)
    return { id: "quality_gate", name: "Quality gate before deploy", status: "off", detail: "No quality-gate report for this build." };
  const rate = `${Math.round(report.pass_rate * 100)}% mean pass rate`;
  return report.passed
    ? { id: "quality_gate", name: "Quality gate before deploy", status: "on", detail: `The ${report.live ? "live" : "offline"} eval gate passed (${rate}).` }
    : { id: "quality_gate", name: "Quality gate before deploy", status: "partial", detail: `The latest ${report.live ? "live" : "offline"} eval gate failed (${rate}).` };
}

export interface CoverageRow {
  ref: string;
  title: string;
  status: ControlStatus | "none";
  evidence: Control[];
}

function combine(statuses: ControlStatus[]): ControlStatus | "none" {
  if (!statuses.length) return "none";
  if (statuses.every((s) => s === "on")) return "on";
  if (statuses.every((s) => s === "off")) return "off";
  return "partial";
}

/** Every reference of one framework, with the status of the controls that support it on this agent. */
export function coverage(framework: FrameworkId, controls: Control[]): CoverageRow[] {
  return Object.entries(REFERENCES[framework]).map(([ref, title]) => {
    const evidence = controls.filter((c) => (MAPPING[c.id]?.[framework] ?? []).includes(ref));
    return { ref, title, status: combine(evidence.map((c) => c.status)), evidence };
  });
}

export function coverageSummary(rows: CoverageRow[]): { on: number; partial: number; off: number; none: number; total: number } {
  const count = (s: CoverageRow["status"]) => rows.filter((r) => r.status === s).length;
  return { on: count("on"), partial: count("partial"), off: count("off"), none: count("none"), total: rows.length };
}

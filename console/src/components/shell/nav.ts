import {
  Activity,
  BookOpen,
  Blocks,
  ClipboardCheck,
  History,
  LayoutDashboard,
  Layers,
  Library,
  MessageSquareText,
  Rocket,
  Settings2,
  Shield,
  UserCheck,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  match?: (path: string) => boolean;
  external?: boolean;
}

export const PRIMARY_NAV: NavItem[] = [
  { to: "/", label: "Overview", icon: LayoutDashboard, match: (p) => p === "/" },
  { to: "/agents", label: "Agents", icon: Blocks, match: (p) => p.startsWith("/agents") },
  { to: "/playground", label: "Playground", icon: MessageSquareText },
  { to: "/evaluations", label: "Evaluations", icon: ClipboardCheck },
  { to: "/knowledge", label: "Knowledge", icon: Library },
  { to: "/approvals", label: "Approvals", icon: UserCheck },
  { to: "/sessions", label: "Sessions", icon: History },
  { to: "/telemetry", label: "Telemetry", icon: Activity },
  { to: "/security", label: "Security", icon: Shield },
  { to: "/deployments", label: "Deployments", icon: Rocket },
];

export const SECONDARY_NAV: NavItem[] = [
  { to: "/platform", label: "Platform", icon: Layers },
  { to: "/settings", label: "Settings", icon: Settings2 },
];

export const DOCS_ICON = BookOpen;

export function isActive(item: NavItem, path: string): boolean {
  return item.match ? item.match(path) : path === item.to || path.startsWith(item.to + "/");
}

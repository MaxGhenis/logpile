"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  IconNotebook,
  IconList,
  IconUsers,
  IconBook,
  IconApi,
  IconGitBranch,
  IconShieldCheck,
} from "@tabler/icons-react";

type NavItem = { href: string; label: string; icon: typeof IconNotebook; privateOnly?: boolean };

const NAV: readonly NavItem[] = [
  { href: "/", label: "Record", icon: IconNotebook },
  { href: "/sessions", label: "Sessions", icon: IconList },
  { href: "/repos", label: "Repos", icon: IconGitBranch },
  { href: "/u", label: "Operators", icon: IconUsers },
  { href: "/analysis", label: "Ledger", icon: IconBook },
  { href: "/publish", label: "Publish", icon: IconShieldCheck, privateOnly: true },
] as const;

export function Sidebar({ publicMode = false }: { publicMode?: boolean }) {
  const pathname = usePathname();
  const visibleNav = NAV.filter((item) => !(item.privateOnly && publicMode));

  function isActive(href: string) {
    if (href === "/") return pathname === "/";
    return pathname.startsWith(href);
  }

  return (
    <nav className="w-[220px] min-h-screen bg-gradient-to-b from-[#1a1614] to-[#141210] border-r border-lp-border-dim flex flex-col shrink-0 sticky top-0 h-screen overflow-y-auto">
      {/* Brand */}
      <Link href="/" className="flex items-center gap-2.5 px-[18px] pt-5 pb-4 no-underline">
        <div className="brand-mark">
          <span /><span /><span />
        </div>
        <span className="font-brand text-xl font-bold text-lp-text tracking-tight">
          Logpile
        </span>
      </Link>

      {/* Nav */}
      <ul className="list-none flex-1 py-1.5">
        {visibleNav.map(({ href, label, icon: Icon }) => (
          <li key={href}>
            <Link
              href={href}
              className={`flex items-center gap-2.5 px-[18px] py-2.5 text-[0.88rem] font-medium border-l-2 transition-all no-underline ${
                isActive(href)
                  ? "text-lp-amber bg-lp-amber-glow border-l-lp-amber"
                  : "text-lp-text-dim border-l-transparent hover:text-lp-text hover:bg-white/[0.03]"
              }`}
            >
              <Icon size={18} stroke={1.5} className={isActive(href) ? "opacity-100" : "opacity-70"} />
              {label}
            </Link>
          </li>
        ))}
      </ul>

      {/* Footer */}
      <div className="px-[18px] py-3.5 border-t border-lp-border-dim">
        <div className="text-[0.68rem] text-lp-text-faint tracking-wide mb-1.5 italic">
          the record of agentic work
        </div>
        <Link
          href="/api/sessions"
          className="text-xs text-lp-text-faint font-mono tracking-wider hover:text-lp-amber no-underline"
        >
          <IconApi size={12} className="inline mr-1 -mt-0.5" />
          /api
        </Link>
      </div>
    </nav>
  );
}

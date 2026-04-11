import {
  IconPencil,
  IconTestPipe,
  IconHammer,
  IconGitCommit,
} from "@tabler/icons-react";

interface ActivityBadgesProps {
  write_path_count?: number | null;
  test_run_count?: number | null;
  test_failure_count?: number | null;
  build_run_count?: number | null;
  build_failure_count?: number | null;
  git_commit_count?: number | null;
}

export function ActivityBadges(props: ActivityBadgesProps) {
  const badges: { icon: React.ReactNode; label: string; variant: "default" | "danger" }[] = [];

  if ((props.write_path_count ?? 0) > 0) {
    badges.push({
      icon: <IconPencil size={11} stroke={2} />,
      label: `${props.write_path_count} writes`,
      variant: "default",
    });
  }
  if ((props.test_run_count ?? 0) > 0) {
    const failures = props.test_failure_count ?? 0;
    badges.push({
      icon: <IconTestPipe size={11} stroke={2} />,
      label: failures > 0 ? `${failures}/${props.test_run_count} failed` : `${props.test_run_count} tests`,
      variant: failures > 0 ? "danger" : "default",
    });
  }
  if ((props.build_run_count ?? 0) > 0) {
    const failures = props.build_failure_count ?? 0;
    badges.push({
      icon: <IconHammer size={11} stroke={2} />,
      label: failures > 0 ? `${failures}/${props.build_run_count} failed` : `${props.build_run_count} builds`,
      variant: failures > 0 ? "danger" : "default",
    });
  }
  if ((props.git_commit_count ?? 0) > 0) {
    badges.push({
      icon: <IconGitCommit size={11} stroke={2} />,
      label: `${props.git_commit_count} commits`,
      variant: "default",
    });
  }

  if (badges.length === 0) return null;

  return (
    <div className="flex gap-1.5 flex-wrap">
      {badges.map((b, i) => (
        <span
          key={i}
          className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[0.65rem] font-medium leading-none ${
            b.variant === "danger"
              ? "bg-lp-red/10 text-lp-red border border-lp-red/20"
              : "bg-lp-raised text-lp-text-dim border border-lp-border-dim"
          }`}
        >
          {b.icon}
          {b.label}
        </span>
      ))}
    </div>
  );
}

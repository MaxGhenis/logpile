export function StatCard({
  value,
  label,
}: {
  value: string | number;
  label: string;
}) {
  return (
    <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 text-center relative overflow-hidden hover-glow group">
      <div className="absolute top-0 left-0 right-0 h-0.5 bg-gradient-to-r from-transparent via-lp-amber to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-300" />
      <div className="font-mono text-3xl font-medium text-lp-text tracking-tight">
        {value}
      </div>
      <div className="text-[0.72rem] text-lp-text-faint uppercase tracking-widest mt-1.5 font-semibold">
        {label}
      </div>
    </div>
  );
}

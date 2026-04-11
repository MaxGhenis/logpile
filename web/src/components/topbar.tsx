export function Topbar({ title }: { title: string }) {
  return (
    <div className="h-[52px] bg-lp-bg/85 backdrop-blur-lg border-b border-lp-border-dim flex items-center justify-between px-7 sticky top-0 z-50">
      <h1 className="font-brand text-base font-bold text-lp-text-dim tracking-wide">
        {title}
      </h1>
    </div>
  );
}

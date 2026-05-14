export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="border border-dashed border-border rounded-lg p-8 text-center text-muted text-sm">
      {children}
    </div>
  );
}

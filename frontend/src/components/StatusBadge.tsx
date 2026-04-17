interface Props { status: string; }

export function StatusBadge({ status }: Props) {
  return (
    <span className={`badge badge-${status.toLowerCase()}`}>
      {status}
    </span>
  );
}

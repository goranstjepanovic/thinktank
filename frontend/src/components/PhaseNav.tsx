import { useNavigate } from 'react-router-dom';

type Phase = 1 | 2 | 3;

const STEPS: { phase: Phase; label: string }[] = [
  { phase: 1, label: 'Analysis' },
  { phase: 2, label: 'Q&A' },
  { phase: 3, label: 'Build' },
];

interface PhaseNavProps {
  ideaId: string;
  currentPhase: Phase;
}

export function PhaseNav({ ideaId, currentPhase }: PhaseNavProps) {
  const navigate = useNavigate();

  const navTo = (phase: Phase) => {
    if (phase === 1) {
      // Pass state so IdeaDetail skips its smart redirect
      navigate(`/ideas/${ideaId}`, { state: { skipRedirect: true } });
    } else if (phase === 2) {
      navigate(`/ideas/${ideaId}/phase2`);
    } else {
      navigate(`/ideas/${ideaId}/phase3`);
    }
  };

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
      {STEPS.filter(s => s.phase <= currentPhase).map((step, i) => {
        const isCurrent = step.phase === currentPhase;
        return (
          <span key={step.phase} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            {i > 0 && (
              <span style={{ color: 'var(--text2)', fontSize: 12, userSelect: 'none' }}>›</span>
            )}
            {isCurrent ? (
              <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--text)' }}>
                {step.phase}. {step.label}
              </span>
            ) : (
              <button
                onClick={() => navTo(step.phase)}
                style={{
                  background: 'none', border: 'none', cursor: 'pointer',
                  fontSize: 12, color: 'var(--blue)', padding: 0,
                  textDecoration: 'underline', textUnderlineOffset: 2,
                }}
              >
                {step.phase}. {step.label}
              </button>
            )}
          </span>
        );
      })}
    </div>
  );
}

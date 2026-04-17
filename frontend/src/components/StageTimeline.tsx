import type { StageResult } from '../types';
import { STAGE_NAMES } from '../types';

interface Props {
  currentStage: number;
  stageResults: StageResult[];
  branchStatus: string;
}

export function StageTimeline({ currentStage, stageResults, branchStatus }: Props) {
  const getStageState = (index: number): string => {
    const result = stageResults.find((s) => s.stage_index === index);
    if (result) {
      if (result.status === 'COMPLETED') return 'completed';
      if (result.status === 'FAILED') return 'failed';
      if (result.status === 'RUNNING') return 'running';
      if (result.status === 'SKIPPED') return 'skipped';
    }
    // Infer from branch status + currentStage
    if (branchStatus === 'VIABLE' && index <= 7) return 'completed';
    if (index < currentStage) return 'completed';
    if (index === currentStage && (branchStatus === 'RUNNING')) return 'running';
    return 'pending';
  };

  return (
    <div className="stage-timeline">
      {STAGE_NAMES.map((name, i) => {
        const state = getStageState(i);
        return (
          <>
            <div
              key={i}
              className={`stage-dot stage-dot-${state}`}
              title={`${i}: ${name}`}
            >
              {i}
            </div>
            {i < STAGE_NAMES.length - 1 && (
              <div key={`c-${i}`} className="stage-connector" />
            )}
          </>
        );
      })}
    </div>
  );
}

import { create } from 'zustand';
import type { BranchSummary, IdeaDetail, IdeaSummary, PipelineEvent } from '../types';

interface IdeaStore {
  ideas: IdeaSummary[];
  ideaDetails: Record<string, IdeaDetail>;
  setIdeas: (ideas: IdeaSummary[]) => void;
  setIdeaDetail: (idea: IdeaDetail) => void;
  removeIdea: (id: string) => void;
  applyEvent: (event: PipelineEvent) => void;
}

export const useIdeaStore = create<IdeaStore>((set) => ({
  ideas: [],
  ideaDetails: {},

  setIdeas: (ideas) => set({ ideas }),

  setIdeaDetail: (idea) =>
    set((s) => ({ ideaDetails: { ...s.ideaDetails, [idea.id]: idea } })),

  removeIdea: (id) =>
    set((s) => {
      const details = { ...s.ideaDetails };
      delete details[id];
      return { ideas: s.ideas.filter((i) => i.id !== id), ideaDetails: details };
    }),

  applyEvent: (event) =>
    set((state) => {
      const { idea_id, event_type, payload } = event;

      // Update summary list
      const ideas = state.ideas.map((i) => {
        if (i.id !== idea_id) return i;
        if (event_type === 'idea.converged') return { ...i, status: 'CONVERGED' as const };
        if (event_type === 'idea.abandoned') return { ...i, status: 'ABANDONED' as const };
        if (event_type === 'idea.selected') return { ...i, status: 'SELECTED' as const };
        return i;
      });

      // Update detail if loaded
      const detail = state.ideaDetails[idea_id];
      if (!detail) return { ideas };

      let branches = [...detail.branches];

      switch (event_type) {
        case 'branch.spawned': {
          const exists = branches.some((b) => b.id === payload.branch_id);
          if (!exists) {
            branches.push({
              id: payload.branch_id as string,
              branch_index: payload.branch_index as number,
              status: 'QUEUED',
              current_stage: 0,
              approach_summary: (payload.approach_summary as string) ?? null,
              parent_branch_id: (payload.parent_branch_id as string) ?? null,
              failure_reason: null,
              created_at: event.timestamp,
              updated_at: event.timestamp,
            } as BranchSummary);
          }
          break;
        }
        case 'branch.started':
          branches = patchBranch(branches, payload.branch_id as string, { status: 'RUNNING' });
          break;
        case 'branch.viable':
          branches = patchBranch(branches, payload.branch_id as string, { status: 'VIABLE' });
          break;
        case 'branch.failed':
          branches = patchBranch(branches, payload.branch_id as string, {
            status: 'FAILED',
            failure_reason: payload.failure_reason as string,
          });
          break;
        case 'branch.paused':
          branches = patchBranch(branches, payload.branch_id as string, { status: 'PAUSED' });
          break;
        case 'branch.resumed':
          branches = patchBranch(branches, payload.branch_id as string, { status: 'RUNNING' });
          break;
        case 'stage.started':
          branches = patchBranch(branches, payload.branch_id as string, {
            current_stage: payload.stage_index as number,
          });
          break;
      }

      let ideaStatus = detail.status;
      if (event_type === 'idea.converged') ideaStatus = 'CONVERGED';
      if (event_type === 'idea.abandoned') ideaStatus = 'ABANDONED';
      if (event_type === 'idea.selected') ideaStatus = 'SELECTED';

      return {
        ideas,
        ideaDetails: {
          ...state.ideaDetails,
          [idea_id]: { ...detail, status: ideaStatus, branches },
        },
      };
    }),
}));

function patchBranch(
  branches: BranchSummary[],
  branchId: string,
  patch: Partial<BranchSummary>
): BranchSummary[] {
  return branches.map((b) => (b.id === branchId ? { ...b, ...patch } : b));
}

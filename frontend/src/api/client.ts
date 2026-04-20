const BASE = 'http://localhost:8000/api/v1';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json();
}

export const api = {
  // Ideas
  listIdeas: () => request<import('../types').IdeaSummary[]>('/ideas'),
  getIdea: (id: string) => request<import('../types').IdeaDetail>(`/ideas/${id}`),
  createIdea: (body: { name: string; description: string; requirements: string; constraints: string }) =>
    request<import('../types').IdeaDetail>('/ideas', { method: 'POST', body: JSON.stringify(body) }),
  pauseIdea: (id: string) => request<{ status: string }>(`/ideas/${id}/pause`, { method: 'POST' }),
  resumeIdea: (id: string) => request<{ status: string }>(`/ideas/${id}/resume`, { method: 'POST' }),
  abandonIdea: (id: string) => request<{ status: string }>(`/ideas/${id}/abandon`, { method: 'POST' }),
  deleteIdea: (id: string) => request<void>(`/ideas/${id}`, { method: 'DELETE' }),
  selectSolution: (id: string, branchId: string, notes?: string) =>
    request<import('../types').IdeaDetail>(`/ideas/${id}/select/${branchId}`, {
      method: 'POST',
      body: JSON.stringify({ notes: notes ?? '' }),
    }),

  // Branches
  listBranches: (ideaId: string) =>
    request<import('../types').BranchDetail[]>(`/ideas/${ideaId}/branches`),
  getBranch: (ideaId: string, branchId: string) =>
    request<import('../types').BranchDetail>(`/ideas/${ideaId}/branches/${branchId}`),

  // Documents
  listDocuments: (ideaId: string, branchId: string) =>
    request<import('../types').DocumentMeta[]>(`/ideas/${ideaId}/branches/${branchId}/documents`),
  getDocument: (ideaId: string, branchId: string, docType: string) =>
    request<{ doc_type: string; content: string }>(
      `/ideas/${ideaId}/branches/${branchId}/documents/${docType}`
    ),

  // Phase 2
  startPhase2: (id: string) =>
    request<import('../types').Phase2Session>(`/ideas/${id}/phase2`, { method: 'POST', body: '{}' }),
  getPhase2: (id: string) =>
    request<import('../types').Phase2Session>(`/ideas/${id}/phase2`),
  postPhase2Message: (id: string, content: string) =>
    request<{ message_id: string; status: string }>(`/ideas/${id}/phase2/messages`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),
  markPhase2Ready: (id: string) =>
    request<import('../types').Phase2Session>(`/ideas/${id}/phase2/ready`, { method: 'POST', body: '{}' }),
  resetPhase2: (id: string, depth: 'phase3_only' | 'resolution' | 'conversation', deleteOutputDir = false) =>
    request<import('../types').Phase2Session>(`/ideas/${id}/phase2/reset`, {
      method: 'POST',
      body: JSON.stringify({ depth, delete_output_dir: deleteOutputDir }),
    }),

  // Phase 3
  startPhase3: (id: string, mode: 'classic' | 'multi_agent' = 'classic') =>
    request<import('../types').Phase3Session>(`/ideas/${id}/phase3`, { method: 'POST', body: JSON.stringify({ mode }) }),
  getPhase3: (id: string) =>
    request<import('../types').Phase3Session>(`/ideas/${id}/phase3`),
  cancelPhase3: (id: string) =>
    request<{ cancelled: boolean }>(`/ideas/${id}/phase3/cancel`, { method: 'POST', body: '{}' }),
  getPhase3Activity: (id: string) =>
    request<import('../types').Phase3ActivityEvent[]>(`/ideas/${id}/phase3/activity`),
  listPhase3Dir: (id: string, dir?: string) =>
    request<import('../types').Phase3DirList>(`/ideas/${id}/phase3/files?dir=${encodeURIComponent(dir ?? '')}`),
  getPhase3File: (id: string, path: string) =>
    request<{ path: string; content: string; size: number; truncated: boolean }>(
      `/ideas/${id}/phase3/file?path=${encodeURIComponent(path)}`
    ),
  getPhase3Messages: (id: string) =>
    request<import('../types').Phase3ChatMessage[]>(`/ideas/${id}/phase3/messages`),
  sendPhase3Message: (id: string, content: string) =>
    request<import('../types').Phase3ChatMessage>(`/ideas/${id}/phase3/messages`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),
  regeneratePrd: (id: string) =>
    request<{ queued: boolean }>(`/ideas/${id}/phase3/regenerate-prd`, { method: 'POST', body: '{}' }),

  // Audit
  listModelCalls: (ideaId: string, branchId?: string, callType?: string) => {
    const params = new URLSearchParams();
    if (branchId) params.set('branch_id', branchId);
    if (callType) params.set('call_type', callType);
    const qs = params.toString();
    return request<import('../types').ModelCall[]>(`/ideas/${ideaId}/model-calls${qs ? `?${qs}` : ''}`);
  },
  listFailureAnalyses: (ideaId: string) =>
    request<import('../types').FailureAnalysis[]>(`/ideas/${ideaId}/failure-analyses`),

  // Settings
  getSettings: () => request<{ implementations_dir: string }>('/settings'),
  updateSettings: (body: { implementations_dir: string }) =>
    request<{ implementations_dir: string }>('/settings', { method: 'POST', body: JSON.stringify(body) }),
  moveImplementations: (destination: string) =>
    request<{ moved_items: number; updated_sessions: number; implementations_dir: string }>(
      '/settings/move-implementations',
      { method: 'POST', body: JSON.stringify({ destination }) },
    ),
};

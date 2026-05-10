import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Link, Route, Routes } from 'react-router-dom';
import { IdeaDashboard } from './components/IdeaDashboard';
import { IdeaDetail } from './components/IdeaDetail';
import { Phase2Chat } from './components/Phase2Chat';
import { Phase3Implementation } from './components/Phase3Implementation';
import { SolutionDetail } from './components/SolutionDetail';
import { OpsDashboard } from './pages/OpsDashboard';
import { SettingsPage } from './pages/SettingsPage';

const queryClient = new QueryClient();

function TopBar() {
  return (
    <div className="topbar">
      <Link to="/" style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 10 }}>
        <img src="/icon.png" alt="Think Tank" style={{ width: 28, height: 28, borderRadius: 6 }} />
        <span className="topbar-title">Think Tank</span>
      </Link>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginLeft: 'auto' }}>
        <span style={{ fontSize: 12, color: 'var(--text2)' }}>Local AI Idea Analysis</span>
        <Link
          to="/ops"
          title="Operations"
          style={{ fontSize: 12, color: 'var(--text2)', textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 4 }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
          </svg>
          Ops
        </Link>
        <Link
          to="/settings"
          title="Settings"
          style={{ color: 'var(--text2)', display: 'flex', alignItems: 'center', textDecoration: 'none' }}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </Link>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <div className="app-layout">
          <TopBar />
          <Routes>
            <Route path="/" element={<IdeaDashboard />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/ideas/:id" element={<IdeaDetail />} />
            <Route path="/ideas/:ideaId/solutions/:branchId" element={<SolutionDetail />} />
            <Route path="/ideas/:id/phase2" element={<Phase2Chat />} />
            <Route path="/ideas/:id/phase3" element={<Phase3Implementation />} />
            <Route path="/ops" element={<OpsDashboard />} />
          </Routes>
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

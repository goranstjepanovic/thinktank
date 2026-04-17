import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Link, Route, Routes } from 'react-router-dom';
import { IdeaDashboard } from './components/IdeaDashboard';
import { IdeaDetail } from './components/IdeaDetail';
import { Phase2Chat } from './components/Phase2Chat';
import { Phase3Implementation } from './components/Phase3Implementation';
import { SolutionDetail } from './components/SolutionDetail';

const queryClient = new QueryClient();

function TopBar() {
  return (
    <div className="topbar">
      <Link to="/" style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 10 }}>
        <img src="/icon.png" alt="Think Tank" style={{ width: 28, height: 28, borderRadius: 6 }} />
        <span className="topbar-title">Think Tank</span>
      </Link>
      <span style={{ fontSize: 12, color: 'var(--text2)' }}>Local AI Idea Analysis</span>
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
            <Route path="/ideas/:id" element={<IdeaDetail />} />
            <Route path="/ideas/:ideaId/solutions/:branchId" element={<SolutionDetail />} />
            <Route path="/ideas/:id/phase2" element={<Phase2Chat />} />
            <Route path="/ideas/:id/phase3" element={<Phase3Implementation />} />
          </Routes>
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

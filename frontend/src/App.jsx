import { Navigate, Route, Routes } from "react-router-dom";
import WorkspacePage from "./pages/WorkspacePage";
import DraftPage from "./pages/DraftPage";
import AgentsPage from "./pages/AgentsPage";
import OutreachPage from "./pages/OutreachPage";
import SellerScanPage from "./pages/SellerScanPage";
import ItOutreachPage from "./pages/ItOutreachPage";
import ResalePage from "./pages/ResalePage";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/workspace" replace />} />
      <Route path="/workspace" element={<WorkspacePage />} />
      <Route path="/draft" element={<DraftPage />} />
      <Route path="/outreach" element={<OutreachPage />} />
      <Route path="/it-outreach" element={<ItOutreachPage />} />
      <Route path="/seller" element={<SellerScanPage />} />
      <Route path="/resale" element={<ResalePage />} />
      <Route path="/agents" element={<AgentsPage />} />
      <Route path="*" element={<Navigate to="/workspace" replace />} />
    </Routes>
  );
}

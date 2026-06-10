import { Navigate, Route, Routes } from "react-router-dom";
import LandingPage from "./pages/LandingPage";
import WorkspacePage from "./pages/WorkspacePage";
import DraftPage from "./pages/DraftPage";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<LandingPage />} />
      <Route path="/workspace" element={<WorkspacePage />} />
      <Route path="/draft" element={<DraftPage />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

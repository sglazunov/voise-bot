import { Routes, Route, Navigate } from "react-router-dom";
import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/ui";
import Overview from "./pages/Overview";
import Meetings from "./pages/Meetings";
import Weeek from "./pages/Weeek";
import CloudPage from "./pages/Cloud";
import Recorder from "./pages/Recorder";
import Scheduler from "./pages/Scheduler";
import Context from "./pages/Context";
import Recognition from "./pages/Recognition";
import Profile from "./pages/Profile";

export default function App() {
  return (
    <ToastProvider>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Overview />} />
          <Route path="meetings" element={<Meetings />} />
          <Route path="weeek" element={<Weeek />} />
          <Route path="cloud" element={<CloudPage />} />
          <Route path="recorder" element={<Recorder />} />
          <Route path="scheduler" element={<Scheduler />} />
          <Route path="recognition" element={<Recognition />} />
          <Route path="context" element={<Context />} />
          <Route path="profile" element={<Profile />} />
          {/* Any unknown/old URL falls back to the dashboard. */}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </ToastProvider>
  );
}

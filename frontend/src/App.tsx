import { lazy, Suspense } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Home from "./pages/Home";
import Results from "./pages/Results";
import JourneyPage from "./pages/JourneyPage";
import Confirmation from "./pages/Confirmation";
import MyBookings from "./pages/MyBookings";
import Notice, { Spinner } from "./components/Notice";

// The control room pulls in the charting library; load it only when opened.
const Admin = lazy(() => import("./pages/Admin"));

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Home />} />
          <Route path="search" element={<Results />} />
          <Route path="journey/:id" element={<JourneyPage />} />
          <Route path="booking/:id" element={<Confirmation />} />
          <Route path="bookings" element={<MyBookings />} />
          <Route path="control" element={<Suspense fallback={<Spinner label="Opening control room" />}><Admin /></Suspense>} />
          <Route path="*" element={<Notice tone="empty" title="Page not found">Use the menu above to get back to booking.</Notice>} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

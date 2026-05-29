import { useState } from "react";
import { BrowserRouter, Routes, Route, NavLink } from "react-router-dom";
import { ProfileProvider, useProfile } from "./ProfileContext.jsx";
import Home from "./pages/Home.jsx";
import TrainingPlanPage from "./pages/TrainingPlanPage.jsx";
import RunningPage from "./pages/RunningPage.jsx";
import LiftingPage from "./pages/LiftingPage.jsx";
import NutritionPage from "./pages/NutritionPage.jsx";

const NAV = [
  { to: "/", label: "Home", end: true },
  { to: "/plan", label: "Plan" },
  { to: "/running", label: "Running" },
  { to: "/lifting", label: "Lifting" },
  { to: "/nutrition", label: "Nutrition" },
];

function Brand() {
  const { active, profiles, setActiveId } = useProfile();
  const [open, setOpen] = useState(false);
  return (
    <div className="brand-wrap">
      <button className="brand" onClick={() => setOpen((o) => !o)}>
        Evansgale <span className="brand-profile">{active.name} ▾</span>
      </button>
      {open && (
        <div className="brand-menu">
          {profiles.map((p) => (
            <button
              key={p.id}
              className={p.id === active.id ? "active" : ""}
              onClick={() => {
                setActiveId(p.id);
                setOpen(false);
              }}
            >
              {p.name}
              {!p.dataReady && <span className="brand-soon">coming soon</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Nav() {
  return (
    <nav className="topnav">
      <div className="topnav-inner">
        <Brand />
        <div className="topnav-links">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) => (isActive ? "active" : "")}
            >
              {n.label}
            </NavLink>
          ))}
        </div>
      </div>
    </nav>
  );
}

function Main() {
  const { active } = useProfile();
  return (
    <div className="wrap">
      {active.dataReady ? (
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/plan" element={<TrainingPlanPage />} />
          <Route path="/running" element={<RunningPage />} />
          <Route path="/lifting" element={<LiftingPage />} />
          <Route path="/nutrition" element={<NutritionPage />} />
          <Route path="*" element={<div className="loading">Page not found.</div>} />
        </Routes>
      ) : (
        <>
          <header>
            <h1>{active.name}</h1>
            <p>Training overview</p>
          </header>
          <div className="card">
            <h2>{active.name}'s profile is not connected yet</h2>
            <p className="sub">
              This is where {active.name}'s running, lifting and nutrition will appear
              once her Strava and Strong exports are wired into the data pipeline. Switch
              back to Luke from the Evansgale menu to see the live dashboard.
            </p>
          </div>
        </>
      )}
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <ProfileProvider>
        <Nav />
        <Main />
      </ProfileProvider>
    </BrowserRouter>
  );
}

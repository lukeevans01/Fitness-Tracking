import { createContext, useContext, useState } from "react";

// Household profiles. "Evansgale" is the family name; each person has their own
// training data. Luke's pipeline is wired now; Maddie's data is not connected yet,
// so her profile renders a friendly placeholder instead of showing Luke's numbers.
export const PROFILES = [
  { id: "luke", name: "Luke", dataReady: true },
  { id: "maddie", name: "Maddie", dataReady: false },
];

const ProfileCtx = createContext({
  active: PROFILES[0],
  profiles: PROFILES,
  setActiveId: () => {},
});

export function ProfileProvider({ children }) {
  const [activeId, setActiveId] = useState(PROFILES[0].id);
  const active = PROFILES.find((p) => p.id === activeId) || PROFILES[0];
  return (
    <ProfileCtx.Provider value={{ active, profiles: PROFILES, setActiveId }}>
      {children}
    </ProfileCtx.Provider>
  );
}

export function useProfile() {
  return useContext(ProfileCtx);
}

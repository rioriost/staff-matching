import "./icons.css";

const glyphs = {
  briefcase: "M9 7V4h6v3 M4 7h16v13H4Z M4 12c5 3 11 3 16 0 M10 13v3h4v-3",
  people: "M9 4a3 3 0 1 0 0 6 3 3 0 0 0 0-6 M3 20v-3a6 6 0 0 1 12 0v3 M16 5a3 3 0 0 1 0 6 M18 14a5 5 0 0 1 3 5v1",
  person: "M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M4 21v-3a8 8 0 0 1 16 0v3",
  building: "M4 21V4h11v17 M15 10h5v11 M8 8h3 M8 12h3 M8 16h3 M9 21v-2 M2 21h20",
  match: "M7 6h10 M7 18h10 M7 8l10 8 M4 3h3v6H4Z M17 3h3v6h-3Z M4 15h3v6H4Z M17 15h3v6h-3Z",
  introduction: "M4 7h15l-4-4 M20 17H5l4 4 M19 7l-4 4 M5 17l4-4",
  calendar: "M5 5h14v16H5Z M8 3v5 M16 3v5 M5 10h14 M9 14h1 M14 14h1 M9 17h1",
  inbox: "M5 4h14l3 10v6H2v-6Z M2 14h6l2 3h4l2-3h6",
  mail: "M3 5h18v14H3Z M3 6l9 7 9-7",
  search: "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M15 15l6 6",
  refresh: "M19 8A8 8 0 1 0 20 15 M19 3v5h-5",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v5l3 2",
  location: "M12 22s7-8 7-13A7 7 0 0 0 5 9c0 5 7 13 7 13Z M12 6a3 3 0 1 0 0 6 3 3 0 0 0 0-6",
  route: "M5 3a2 2 0 1 0 0 4 2 2 0 0 0 0-4 M19 17a2 2 0 1 0 0 4 2 2 0 0 0 0-4 M5 7v3c0 3 14 1 14 5v2",
  pin: "M8 3h8l-1 7 4 4H5l4-4Z M12 14v8",
  left: "M14 5l-7 7 7 7",
  right: "M10 5l7 7-7 7",
  check: "M4 12l5 5L20 6",
  checkCircle: "M21 11v1a9 9 0 1 1-5-8 M9 11l3 3 9-10",
  warning: "M12 3L2 21h20Z M12 9v5 M12 17v1",
  info: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 11v6 M12 7v1",
  close: "M6 6l12 12 M18 6L6 18",
  star: "M12 3l3 6 7 1-5 5 1 7-6-3-6 3 1-7-5-5 7-1Z",
  shield: "M12 2l8 3v7c0 5-8 10-8 10S4 17 4 12V5Z M8 11l3 3 5-6",
  lock: "M7 10V7a5 5 0 0 1 10 0v3 M5 10h14v11H5Z M12 14v3",
  key: "M8 3a5 5 0 1 0 0 10 5 5 0 0 0 0-10 M12 12l9 9 M16 16l3-3 M18 18l3-3",
  logout: "M10 3H4v18h6 M10 12h11l-4-4 M21 12l-4 4",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6",
  yen: "M5 3l7 9 7-9 M12 12v10 M6 13h12 M6 17h12",
  compare: "M4 4h6v16H4Z M14 4h6v16h-6Z M7 8v8 M17 8v8",
  history: "M4 10a8 8 0 1 1 1 8 M4 4v6h6 M12 7v6l4 2",
} as const;

export type IconName = keyof typeof glyphs;

// Icons supplement visible labels; they never provide a control's accessible name.
export default function Icon({ name, className = "" }: { name: IconName; className?: string }) {
  return <svg className={`ui-icon ${className}`} viewBox="0 0 24 24" width="20" height="20"
    fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
    aria-hidden="true" focusable="false"><path d={glyphs[name]} /></svg>;
}

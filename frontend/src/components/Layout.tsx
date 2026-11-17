import { NavLink, Outlet } from "react-router-dom";

const link = ({ isActive }: { isActive: boolean }) =>
  `px-3 py-2 rounded-md text-[15px] font-medium transition-colors ${
    isActive ? "bg-white/15 text-white" : "text-white/75 hover:text-white"
  }`;

export default function Layout() {
  return (
    <div className="min-h-screen flex flex-col">
      <header className="bg-ink text-white">
        <div className="mx-auto max-w-6xl px-4 h-16 flex items-center gap-6">
          <NavLink to="/" className="flex items-center gap-2.5 shrink-0" aria-label="TatkalX home">
            <span className="flex gap-1" aria-hidden>
              <i className="block size-2.5 rounded-full bg-proceed" />
              <i className="block size-2.5 rounded-full bg-caution" />
              <i className="block size-2.5 rounded-full bg-[#e0484a]" />
            </span>
            <span className="font-display text-2xl font-bold tracking-wide">TatkalX</span>
          </NavLink>
          <nav className="flex gap-1 overflow-x-auto" aria-label="Main">
            <NavLink to="/" end className={link}>Book</NavLink>
            <NavLink to="/bookings" className={link}>My bookings</NavLink>
            <NavLink to="/control" className={link}>Control room</NavLink>
          </nav>
        </div>
      </header>
      <div className="bg-caution-soft border-b border-caution/40 text-[14px] text-ink">
        <p className="mx-auto max-w-6xl px-4 py-1.5">
          Simulation for a distributed-systems project. Trains, fares and tickets are not real and nothing
          connects to any railway reservation system.
        </p>
      </div>
      <main className="flex-1 mx-auto w-full max-w-6xl px-4 py-8">
        <Outlet />
      </main>
      <footer className="border-t border-rule text-ink-soft text-sm">
        <div className="mx-auto max-w-6xl px-4 py-5">
          TatkalX runs three FastAPI nodes behind Nginx. PostgreSQL decides every booking; Redis only coordinates.
        </div>
      </footer>
    </div>
  );
}

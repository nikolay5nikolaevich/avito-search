import { Link } from "react-router-dom";

// Верхняя навигация рабочих экранов (workspace/draft).
// links — массив { to, label }; aria-label у страниц различается.
export default function Topbar({ ariaLabel, links }) {
  return (
    <nav className="topbar" aria-label={ariaLabel}>
      <span className="topbar-wordmark">AVITO RESEARCH</span>
      <div className="draft-topbar-nav">
        {links.map((link) => (
          <Link key={link.to} to={link.to} className="secondary-button">
            {link.label}
          </Link>
        ))}
      </div>
    </nav>
  );
}

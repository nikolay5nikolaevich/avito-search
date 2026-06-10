import { Link } from "react-router-dom";

// Футер рабочих экранов: копирайт + навигационные ссылки ({ to, label }).
export default function SiteFooter({ links }) {
  return (
    <footer className="site-footer workspace-footer" role="contentinfo">
      <p className="footer-left">© 2026 Avito Research</p>
      <nav className="footer-right" aria-label="Навигация футера">
        {links.map((link) => (
          <Link key={link.to} to={link.to} className="footer-link">
            {link.label}
          </Link>
        ))}
      </nav>
    </footer>
  );
}

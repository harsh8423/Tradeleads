import React, { useState } from 'react';
import { DiscoverCard } from './types';
import { qualityClass, contactTypeIcon, copyText } from './utils';

interface LeadCardProps {
    card: DiscoverCard;
    isDragging: boolean;
    dragX: number;
    exitClass: string;
    cardRef: React.RefObject<HTMLDivElement | null>;
    onDragStart: (x: number) => void;
    onDragMove: (x: number) => void;
    onDragEnd: () => void;
    handleSwipe: (action: "save" | "skip") => void;
}

export function LeadCard({
    card, isDragging, dragX, exitClass, cardRef,
    onDragStart, onDragMove, onDragEnd, handleSwipe
}: LeadCardProps) {
    const [showAllContacts, setShowAllContacts] = useState(false);

    const cardStyle = isDragging ? {
        transform: `translateX(calc(-50% + ${dragX}px)) rotate(${dragX * 0.05}deg)`,
        transition: "none",
    } : {};

    const skipHintOpacity = isDragging ? Math.max(0, Math.min(1, -dragX / 80)) : 0;
    const saveHintOpacity = isDragging ? Math.max(0, Math.min(1, dragX / 80)) : 0;

    return (
        <div
            ref={cardRef as React.RefObject<HTMLDivElement>}
            className={`lead-card ${isDragging ? "is-dragging" : ""} ${exitClass} enter`}
            style={cardStyle}
            onMouseDown={e => onDragStart(e.clientX)}
            onMouseMove={e => isDragging && onDragMove(e.clientX)}
            onMouseUp={onDragEnd}
            onMouseLeave={onDragEnd}
            onTouchStart={e => onDragStart(e.touches[0].clientX)}
            onTouchMove={e => onDragMove(e.touches[0].clientX)}
            onTouchEnd={onDragEnd}
            role="article"
            aria-label={`Lead card for ${card.domain}`}
        >
            <div className="swipe-hint skip" style={{ opacity: skipHintOpacity }}>SKIP</div>
            <div className="swipe-hint save" style={{ opacity: saveHintOpacity }}>SAVE</div>

            <div className="card-domain" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <a
                    href={`https://${card.domain}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{ color: "inherit", textDecoration: "none" }}
                    onClick={e => e.stopPropagation()}
                >
                    🌐 {card.domain}
                </a>
                {card.has_employees && (
                    <span className="tag tag-purple" style={{ fontSize: "0.6rem" }}>👥 Team</span>
                )}
            </div>

            <div className="card-title">
                {card.title || card.domain}
            </div>

            {card.description && (
                <div className="card-desc">{card.description}</div>
            )}

            <div className="card-meta">
                {card.category && (
                    <span className="tag tag-blue">{card.category}</span>
                )}
                {card.location && (
                    <span className="tag tag-amber">📍 {card.location.split(",")[0]}</span>
                )}
            </div>

            <div className="quality-bar-wrap" style={{ marginBottom: 16 }}>
                <span style={{ fontSize: "0.7rem", color: "var(--text-muted)", minWidth: 80 }}>
                    Lead Quality
                </span>
                <div className="quality-bar">
                    <div
                        className={`quality-bar-fill ${qualityClass(card.quality_score)}`}
                        style={{ width: `${Math.min(100, card.quality_score)}%` }}
                    />
                </div>
                <span className={`quality-label ${qualityClass(card.quality_score)}`}>
                    {card.quality_score}
                </span>
            </div>

            <div className="card-contacts-row">
                {card.email_count > 0 && (
                    <div className="contact-badge email" onClick={(e) => { e.stopPropagation(); setShowAllContacts(true); }}>
                        📧 {card.email_count} Email{card.email_count > 1 ? "s" : ""}
                    </div>
                )}
                {card.phone_count > 0 && (
                    <div className="contact-badge phone" onClick={(e) => { e.stopPropagation(); setShowAllContacts(true); }}>
                        📞 {card.phone_count} Phone{card.phone_count > 1 ? "s" : ""}
                    </div>
                )}
                {card.linkedin_count > 0 && (
                    <div className="contact-badge linkedin" onClick={(e) => { e.stopPropagation(); setShowAllContacts(true); }}>
                        🔗 LinkedIn
                    </div>
                )}
                {card.social_count > 0 && (
                    <div className="contact-badge social" onClick={(e) => { e.stopPropagation(); setShowAllContacts(true); }}>
                        🌐 {card.social_count} Social
                    </div>
                )}
                {card.email_count === 0 && card.phone_count === 0 && (
                    <span style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                        No contacts found
                    </span>
                )}
            </div>

            {card.top_contacts.length > 0 && (
                <div className="card-top-contacts">
                    {card.top_contacts.slice(0, showAllContacts ? undefined : 3).map((c, i) => (
                        <div key={i} className="contact-row">
                            <span className="contact-row-type">
                                {contactTypeIcon(c.entity_type)} {c.entity_type}
                            </span>
                            <span
                                className="contact-row-value"
                                onClick={() => copyText(c.value)}
                                title={`Click to copy: ${c.value}`}
                            >
                                {c.value}
                            </span>
                        </div>
                    ))}
                    {card.top_contacts.length > 3 && (
                        <button
                            type="button"
                            className="btn btn-secondary"
                            style={{ padding: "4px 8px", fontSize: "0.75rem", marginTop: 8, width: "100%" }}
                            onClick={(e) => { e.stopPropagation(); setShowAllContacts(!showAllContacts); }}
                        >
                            {showAllContacts ? "Hide Contacts" : `View all ${card.top_contacts.length} Contacts`}
                        </button>
                    )}
                </div>
            )}

            <div className="card-actions">
                <button
                    className="swipe-btn skip"
                    onClick={() => handleSwipe("skip")}
                    title="Skip (← arrow key)"
                >
                    <div style={{ fontSize: "1.2rem" }}>✕</div>
                    <div style={{ fontSize: "0.7rem", marginTop: 2 }}>Skip</div>
                </button>
                <a
                    href={`https://${card.domain}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="card-view-btn"
                    onClick={e => e.stopPropagation()}
                >
                    🔎 View Company
                </a>
                <button
                    className="swipe-btn save"
                    onClick={() => handleSwipe("save")}
                    title="Save (→ arrow key)"
                >
                    <div style={{ fontSize: "1.2rem" }}>♥</div>
                    <div style={{ fontSize: "0.7rem", marginTop: 2 }}>Save</div>
                </button>
            </div>
        </div>
    );
}

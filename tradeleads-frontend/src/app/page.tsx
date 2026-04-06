"use client";
import { useEffect, useState, useRef, useCallback } from "react";
import { DiscoverResponse, DiscoverCard, SavedLead } from "../components/types";
import { showToast, fmt } from "../components/utils";
import { SearchPanel } from "../components/SearchPanel";
import { LeadCard } from "../components/LeadCard";
import { SavedDrawer } from "../components/SavedDrawer";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export default function Home() {
  const [aiMode, setAiMode] = useState(true);

  // Search params
  const [keyword, setKeyword] = useState("");
  const [category, setCategory] = useState<string>("");
  const [country, setCountry] = useState("");
  const [contactType, setContactType] = useState<string>("");

  // Feed state
  const [cards, setCards] = useState<DiscoverCard[]>([]);
  const [seenDomains, setSeenDomains] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [totalMatched, setTotalMatched] = useState(0);
  const [hasSearched, setHasSearched] = useState(false);
  const [error, setError] = useState("");

  // Saved leads
  const [savedLeads, setSavedLeads] = useState<SavedLead[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);

  // Current active card index
  const [currentIdx, setCurrentIdx] = useState(0);
  const [exitClass, setExitClass] = useState<"exit-left" | "exit-right" | "">("");
  const [isDragging, setIsDragging] = useState(false);
  const [dragX, setDragX] = useState(0);
  const [dragStart, setDragStart] = useState(0);

  const cardRef = useRef<HTMLDivElement>(null);

  // Keyboard navigation
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (cards.length === 0 || currentIdx >= cards.length) return;
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === "ArrowLeft") handleSwipe("skip");
      if (e.key === "ArrowRight") handleSwipe("save");
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cards, currentIdx, seenDomains]);

  // Auto-load more when stack runs low
  useEffect(() => {
    const remaining = cards.length - currentIdx;
    if (remaining <= 2 && hasMore && !loading && hasSearched) {
      fetchMore();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentIdx, cards.length, hasMore, loading]);

  // ── Fetch ──

  const buildRequest = (excluded: string[]) => ({
    keyword: keyword || undefined,
    category: category ? [category] : undefined,
    country: country || undefined,
    contact_type: contactType ? [contactType] : undefined,
    excluded_domains: excluded,
    batch_size: 10,
  });

  const fetchBatch = async (excluded: string[], isFirst: boolean) => {
    setLoading(true);
    setError("");
    try {
      const endpoint = aiMode && isFirst ? "/leads/discover/ai" : "/leads/discover";

      let body: any;
      if (aiMode && isFirst) {
        body = { query: keyword || "trade logistics companies with contact info", limit: 10 };
      } else {
        body = buildRequest(excluded);
      }

      const res = await fetch(`${API}${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data: DiscoverResponse = await res.json();
      if (!res.ok) throw new Error((data as any).detail || "Request failed");

      const newCards = data.cards || [];
      const newDomains = newCards.map((c: DiscoverCard) => c.domain);

      setCards(prev => isFirst ? newCards : [...prev, ...newCards]);
      setSeenDomains(prev => {
        const next = isFirst ? newDomains : [...prev, ...newDomains];
        return next;
      });
      setHasMore(data.has_more ?? false);
      setTotalMatched(data.total_matched);
      if (isFirst) setCurrentIdx(0);
      setHasSearched(true);
    } catch (e: any) {
      setError(e.message);
    }
    setLoading(false);
  };

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    setCards([]);
    setSeenDomains([]);
    setCurrentIdx(0);
    fetchBatch([], true);
  };

  const fetchMore = useCallback(() => {
    if (!hasMore || loading) return;
    fetchBatch(seenDomains, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seenDomains, hasMore, loading]);

  // ── Swipe Logic ──

  const handleSwipe = (action: "skip" | "save") => {
    if (currentIdx >= cards.length) return;
    const card = cards[currentIdx];

    if (action === "save") {
      setSavedLeads(prev => {
        if (prev.find(l => l.domain === card.domain)) return prev;
        return [...prev, {
          domain: card.domain,
          title: card.title,
          category: card.category,
          quality_score: card.quality_score,
        }];
      });
      showToast(`✅ Saved ${card.domain}`, "saved");
    } else {
      showToast(`⏭ Skipped`, "skipped");
    }

    setExitClass(action === "skip" ? "exit-left" : "exit-right");
    setTimeout(() => {
      setExitClass("");
      setCurrentIdx(prev => prev + 1);
      setDragX(0);
    }, 350);
  };

  // Mouse/touch drag
  const onDragStart = (clientX: number) => {
    setDragStart(clientX);
    setIsDragging(true);
  };
  const onDragMove = (clientX: number) => {
    if (!isDragging) return;
    setDragX(clientX - dragStart);
  };
  const onDragEnd = () => {
    if (!isDragging) return;
    setIsDragging(false);
    const threshold = 80;
    if (dragX < -threshold) handleSwipe("skip");
    else if (dragX > threshold) handleSwipe("save");
    else setDragX(0);
  };

  const currentCard = cards[currentIdx];
  const remaining = cards.length - currentIdx;
  const isDone = hasSearched && remaining === 0 && !hasMore;
  const isSearching = loading && cards.length === 0;

  return (
    <main>
      {/* Hero */}
      <section className="hero container">
        <h1 className="hero-title">
          Find <span>Verified B2B Leads</span><br />for Trade & Logistics
        </h1>
        <p className="hero-sub">
          Discover active companies with emails, phones &amp; social profiles.
          Swipe to save leads instantly.
        </p>
      </section>

      <div className="container" style={{ paddingBottom: 80 }}>
        <SearchPanel
          aiMode={aiMode}
          setAiMode={setAiMode}
          keyword={keyword}
          setKeyword={setKeyword}
          category={category}
          setCategory={setCategory}
          country={country}
          setCountry={setCountry}
          contactType={contactType}
          setContactType={setContactType}
          handleSearch={handleSearch}
          loading={loading}
          hasSearched={hasSearched}
          onReset={() => { setCards([]); setSeenDomains([]); setCurrentIdx(0); setHasSearched(false); }}
          savedCount={savedLeads.length}
          openDrawer={() => setDrawerOpen(true)}
          isInitialSearchAction={loading && cards.length === 0}
        />

        {error && (
          <div style={{
            background: "var(--red-light)", border: "1px solid rgba(239,68,68,0.3)",
            borderRadius: "var(--radius-sm)", padding: "12px 16px", marginBottom: 20,
            color: "var(--red)", fontSize: "0.875rem"
          }}>
            ❌ {error}
          </div>
        )}

        {hasSearched && (
          <div>
            <div className="feed-header">
              <div>
                <div className="section-title">Lead Discovery Feed</div>
                <div className="section-sub feed-counter">
                  {isDone
                    ? "You've seen all matching leads"
                    : <>Showing card <strong>{currentIdx + 1}</strong> of <strong>{fmt(totalMatched)}</strong> matches</>
                  }
                </div>
              </div>
              {hasMore && remaining <= 3 && (
                <button className="btn btn-secondary" onClick={fetchMore} disabled={loading}>
                  {loading ? "Loading…" : "Load More"}
                </button>
              )}
            </div>

            {isSearching && (
              <div className="card-stack-area">
                <div style={{ maxWidth: 680, width: "100%" }}>
                  <div className="loading-bar" />
                  <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: "var(--radius)", padding: 28, boxShadow: "var(--shadow-card)" }}>
                    <div className="skeleton" style={{ height: 14, width: "40%", marginBottom: 12 }} />
                    <div className="skeleton" style={{ height: 24, width: "80%", marginBottom: 16 }} />
                    <div className="skeleton" style={{ height: 14, width: "100%", marginBottom: 8 }} />
                    <div className="skeleton" style={{ height: 14, width: "90%", marginBottom: 24 }} />
                    <div style={{ display: "flex", gap: 8 }}>
                      {[70, 90, 60].map((w, i) => <div key={i} className="skeleton" style={{ height: 28, width: w }} />)}
                    </div>
                  </div>
                </div>
              </div>
            )}

            {isDone && (
              <div className="feed-empty">
                <div className="feed-empty-icon">🎉</div>
                <div className="feed-empty-title">All Leads Discovered!</div>
                <div className="feed-empty-sub">
                  You&apos;ve seen all {fmt(totalMatched)} matching leads.
                  Try changing your filters to find more.
                </div>
                <div style={{ display: "flex", gap: 10, marginTop: 16 }}>
                  <button className="btn btn-secondary" onClick={() => {
                    setCards([]); setSeenDomains([]);
                    setCurrentIdx(0); setHasMore(true);
                    fetchBatch([], true);
                  }}>
                    🔄 Start Over
                  </button>
                  {savedLeads.length > 0 && (
                    <button className="btn btn-success" onClick={() => setDrawerOpen(true)}>
                      🗂 View {savedLeads.length} Saved Leads
                    </button>
                  )}
                </div>
              </div>
            )}

            {!isSearching && !isDone && currentCard && (
              <div className="card-stack-area">
                {cards[currentIdx + 1] && <div className="card-stack-ghost g1" />}
                {cards[currentIdx + 2] && <div className="card-stack-ghost g2" />}

                <LeadCard
                  card={currentCard}
                  isDragging={isDragging}
                  dragX={dragX}
                  exitClass={exitClass}
                  cardRef={cardRef}
                  onDragStart={onDragStart}
                  onDragMove={onDragMove}
                  onDragEnd={onDragEnd}
                  handleSwipe={handleSwipe}
                />

                <div className="kbd-hint">
                  <div className="kbd-hint-item"><kbd className="kbd">←</kbd> Skip</div>
                  <div className="kbd-hint-item"><kbd className="kbd">→</kbd> Save</div>
                  <div className="kbd-hint-item"><span style={{ fontSize: "0.72rem" }}>or drag the card</span></div>
                </div>

                {loading && cards.length > 0 && (
                  <div style={{ marginTop: 20, textAlign: "center" }}>
                    <div className="loading-bar" style={{ maxWidth: 200, margin: "0 auto" }} />
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: 8 }}>
                      Loading more leads…
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {!hasSearched && !loading && (
          <div className="feed-empty" style={{ marginTop: 20 }}>
            <div className="feed-empty-icon">🎯</div>
            <div className="feed-empty-title">Start Discovering Leads</div>
            <div className="feed-empty-sub">
              Enter a keyword or use AI Mode to describe what you&apos;re looking for.
              Swipe right to save. Swipe left to skip.
            </div>
          </div>
        )}
      </div>

      <SavedDrawer
        drawerOpen={drawerOpen}
        setDrawerOpen={setDrawerOpen}
        savedLeads={savedLeads}
        setSavedLeads={setSavedLeads}
      />
    </main>
  );
}

import { useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Mail,
  Search,
  Brain,
  BookOpen,
  X,
} from "lucide-react";
import "./App.css";

const API_URL =
  import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8001";

function isPendingMessage(message) {
  return !message.pipeline?.ui_summary?.route;
}

function App() {
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedMessage, setSelectedMessage] = useState(null);
  const [page, setPage] = useState("queue");
  const [feedbackState, setFeedbackState] = useState({});
  const [syncingGmail, setSyncingGmail] = useState(false);
  const [syncNotice, setSyncNotice] = useState(null);

  async function loadMessages() {
    const response = await fetch(`${API_URL}/api/messages`);

    if (!response.ok) {
      throw new Error("Failed to load messages");
    }

    const data = await response.json();
    setMessages(data.messages || []);
  }

  function handleMessageUpdated(messageId, pipeline) {
    setMessages((prev) =>
      prev.map((m) => (m.id === messageId ? { ...m, pipeline } : m))
    );
    setSelectedMessage((prev) =>
      prev && prev.id === messageId ? { ...prev, pipeline } : prev
    );
  }

  async function syncGmail() {
    if (syncingGmail) return;
    setSyncingGmail(true);
    setSyncNotice(null);

    try {
      const response = await fetch(`${API_URL}/api/gmail/sync?limit=10`, {
        method: "POST",
      });

      if (!response.ok) {
        throw new Error("Gmail sync failed");
      }

      const data = await response.json();

      // Refresh the message list so newly imported PENDING messages appear.
      try {
        await loadMessages();
      } catch (refreshErr) {
        console.error(refreshErr);
      }

      const imported = data.imported_count ?? 0;
      const skipped = data.skipped_count ?? 0;
      const errors = data.errors ?? [];

      if (errors.length > 0) {
        setSyncNotice({
          kind: "error",
          text: "Gmail sync completed with errors.",
        });
        return;
      }

      let text;
      if (imported > 0 && skipped > 0) {
        text = `Imported ${imported} · Skipped ${skipped}`;
      } else if (imported > 0) {
        text = `Imported ${imported} new message${imported === 1 ? "" : "s"}`;
      } else {
        text = `No new messages · ${skipped} already synced`;
      }

      setSyncNotice({ kind: "success", text });
    } catch (err) {
      console.error(err);
      setSyncNotice({
        kind: "error",
        text: "Could not sync Gmail. Please try again.",
      });
    } finally {
      setSyncingGmail(false);
    }
  }

  useEffect(() => {
    (async () => {
      try {
        await loadMessages();
      } catch (err) {
        console.error(err);
        setError("Could not connect to Attention Buddy API.");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const escalate = messages.filter(
    (message) => message.pipeline?.ui_summary?.route === "ESCALATE_NOW"
  );

  const review = messages.filter(
    (message) => message.pipeline?.ui_summary?.route === "APPROVAL_REQUIRED"
  );

  const handled = messages.filter(
    (message) => message.pipeline?.ui_summary?.route === "AUTO_HANDLE"
  );

  const pending = messages.filter(isPendingMessage);

  if (loading) {
    return <div className="center-state">Loading Attention Buddy...</div>;
  }

  if (error) {
    return <div className="center-state error">{error}</div>;
  }

  if (page === "learning") {
    return (
      <div className="app-shell">
        <Sidebar currentPage="learning" onNavigate={setPage} />
        <LearningPage />
      </div>
    );
  }

  if (page === "business") {
    return (
      <div className="app-shell">
        <Sidebar currentPage="business" onNavigate={setPage} />
        <BusinessKnowledgePage />
      </div>
    );
  }

  if (selectedMessage) {
    return (
      <MessageDetail
        key={selectedMessage.id}
        message={selectedMessage}
        feedbackState={feedbackState}
        onBack={() => setSelectedMessage(null)}
        onMessageUpdated={handleMessageUpdated}
        onSubmitFeedback={(payload) =>
          submitFeedback(
            selectedMessage,
            payload,
            feedbackState,
            setFeedbackState
          )
        }
      />
    );
  }

  return (
    <div className="app-shell">
      <Sidebar currentPage="queue" onNavigate={setPage} />

      <main className="main-content">
        <header className="topbar">
          <div>
            <p className="eyebrow">YOUR ATTENTION TODAY</p>
            <h1>Only what needs you.</h1>
            <p className="subtitle">
              Attention Buddy has processed {messages.length} messages.
            </p>
          </div>

          <div className="search-box">
            <Search size={17} />
            <input placeholder="Search messages" />
          </div>
        </header>

        <section className="stats">
          <Stat
            label="Needs you now"
            value={escalate.length}
            detail="Immediate attention"
          />
          <Stat
            label="Review"
            value={review.length}
            detail="Your approval needed"
          />
          <Stat
            label="Handled for you"
            value={handled.length}
            detail="No interruption"
          />
          <Stat
            label="Attention protected"
            value={`${Math.round(
              (handled.length / Math.max(messages.length, 1)) * 100
            )}%`}
            detail="Automatically handled"
          />
        </section>

        <MessageSection
          title="Needs You Now"
          subtitle="These messages crossed your attention boundary."
          messages={escalate}
          type="urgent"
          icon={<AlertTriangle size={19} />}
          onSelectMessage={setSelectedMessage}
        />

        <MessageSection
          title="Review"
          subtitle="Attention Buddy prepared the work. You make the call."
          messages={review}
          type="review"
          icon={<Clock3 size={19} />}
          onSelectMessage={setSelectedMessage}
        />

        <MessageSection
          title="Handled For You"
          subtitle="Routine work that did not need to interrupt you."
          messages={handled}
          type="handled"
          icon={<CheckCircle2 size={19} />}
          onSelectMessage={setSelectedMessage}
        />

        <MessageSection
          title="Pending"
          subtitle="Waiting to be processed by Attention Buddy."
          messages={pending}
          type="pending"
          icon={<Mail size={19} />}
          onSelectMessage={setSelectedMessage}
          action={
            <>
              <button
                type="button"
                className="secondary-action sync-button"
                onClick={syncGmail}
                disabled={syncingGmail}
              >
                {syncingGmail ? "Syncing Gmail..." : "Sync Gmail"}
              </button>
              {syncNotice && (
                <span className={`sync-notice sync-${syncNotice.kind}`}>
                  {syncNotice.text}
                </span>
              )}
            </>
          }
        />
      </main>
    </div>
  );
}

function Sidebar({ currentPage, onNavigate }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-icon">A</div>
        <div>
          <h2>Attention Buddy</h2>
          <span>Founder attention OS</span>
        </div>
      </div>

      <nav>
        <button
          className={`nav-item ${currentPage === "queue" ? "active" : ""}`}
          onClick={() => onNavigate("queue")}
        >
          <Mail size={18} />
          Attention Queue
        </button>

        <button
          className={`nav-item ${currentPage === "learning" ? "active" : ""}`}
          onClick={() => onNavigate("learning")}
        >
          <Brain size={18} />
          Learning
        </button>

        <button
          className={`nav-item ${currentPage === "business" ? "active" : ""}`}
          onClick={() => onNavigate("business")}
        >
          <BookOpen size={18} />
          Business Knowledge
        </button>
      </nav>

      <div className="sidebar-footer">
        <div className="avatar">SP</div>
        <div>
          <strong>Founder</strong>
          <span>Demo workspace</span>
        </div>
      </div>
    </aside>
  );
}

function Stat({ label, value, detail }) {
  return (
    <div className="stat-card">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function MessageSection({
  title,
  subtitle,
  messages,
  type,
  icon,
  onSelectMessage,
  action,
}) {
  return (
    <section className="message-section">
      <div className="section-heading">
        <div className={`section-icon ${type}`}>{icon}</div>
        <div className="section-heading-text">
          <h2>
            {title}
            <span className="count">{messages.length}</span>
          </h2>
          <p>{subtitle}</p>
        </div>
        {action && <div className="section-heading-action">{action}</div>}
      </div>

      <div className="message-list">
        {messages.length === 0 ? (
          <div className="empty-card">Nothing here right now.</div>
        ) : (
          messages.map((message) => (
            <MessageCard
              key={message.id}
              message={message}
              onOpen={() => onSelectMessage(message)}
            />
          ))
        )}
      </div>
    </section>
  );
}

function MessageCard({ message, onOpen }) {
  const summary = message.pipeline?.ui_summary;
  const isPending = isPendingMessage(message);
  const score = Math.round((summary?.attention_score || 0) * 100);

  return (
    <article className="message-card">
      <div className="message-main">
        <div className="sender-row">
          <div>
            <strong>{message.sender_name || "Unknown sender"}</strong>
            <span>{message.sender_address}</span>
          </div>

          {isPending ? (
            <div className="attention-score pending-score">
              <span>Status</span>
              <strong>Pending</strong>
            </div>
          ) : (
            <div className="attention-score">
              <span>Attention</span>
              <strong>{score}%</strong>
            </div>
          )}
        </div>

        <h3>{message.subject}</h3>

        <p className="headline">
          {summary?.headline || message.body_verbatim}
        </p>

        {summary?.why_founder_is_needed && (
          <div className="why-box">
            <strong>Why you're seeing this</strong>
            <p>{summary.why_founder_is_needed}</p>
          </div>
        )}

        <div className="card-footer">
          {isPending ? (
            <span className="route route-PENDING">PENDING</span>
          ) : (
            <span className={`route route-${summary?.route}`}>
              {formatRoute(summary?.route)}
            </span>
          )}

          <button className="review-button" onClick={onOpen}>
            {isPending
              ? "View"
              : summary?.route === "AUTO_HANDLE"
                ? "View"
                : "Review"}
          </button>
        </div>
      </div>

      {!isPending && (
        <div className="score-column">
          <div className="score-track">
            <div
              className="score-fill"
              style={{ height: `${score}%` }}
            />
          </div>
        </div>
      )}
    </article>
  );
}

function MessageDetail({
  message,
  feedbackState,
  onBack,
  onSubmitFeedback,
  onMessageUpdated,
}) {
  const summary = message.pipeline?.ui_summary;
  const isPending = isPendingMessage(message);
  const score = Math.round((summary?.attention_score || 0) * 100);
  const pipelineRunId = message.pipeline?.id;
  const currentDecision =
    summary?.route || message.pipeline?.attention_decision || "AUTO_HANDLE";

  const clioDraft = message.pipeline?.cl_v1?.draft;
  const founderDraft = summary?.founder_draft;
  const currentDraft = founderDraft
    ? {
        subject: founderDraft.subject || "",
        body: founderDraft.body || "",
        edited: true,
      }
    : {
        subject: clioDraft?.subject || "",
        body: clioDraft?.body || "",
        edited: false,
      };

  const communicationStatus = message.pipeline?.communication_status || "";
  const approved = Boolean(summary?.approved);
  const isApprovalRequired = currentDecision === "APPROVAL_REQUIRED";
  const awaitingApproval =
    isApprovalRequired && communicationStatus === "AWAITING_APPROVAL";
  const hasUsableDraft = Boolean(
    currentDraft.body && currentDraft.body.trim()
  );

  const sendStatus = summary?.send_status || "";
  const isExecuted =
    communicationStatus === "EXECUTED" || sendStatus === "EXECUTED";
  const canSend =
    communicationStatus === "READY" &&
    sendStatus === "READY" &&
    hasUsableDraft;

  const [showChangeModal, setShowChangeModal] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [draftSubject, setDraftSubject] = useState(currentDraft.subject);
  const [draftBody, setDraftBody] = useState(currentDraft.body);
  const [savingDraft, setSavingDraft] = useState(false);
  const [approving, setApproving] = useState(false);
  const [draftError, setDraftError] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState("");

  // Pipeline preview state
  const [pipelinePreview, setPipelinePreview] = useState(null);
  const [pipelinePreviewLoading, setPipelinePreviewLoading] = useState(false);
  const [pipelinePreviewError, setPipelinePreviewError] = useState("");

  const msgKey = message.id;
  const feedbackResult = feedbackState[msgKey];

  // Reset pipeline preview when navigating to a different message
  useEffect(() => {
    setPipelinePreview(null);
    setPipelinePreviewError("");
  }, [message.id]);

  const fetchPipelinePreview = async () => {
    setPipelinePreviewLoading(true);
    setPipelinePreviewError("");
    setPipelinePreview(null);

    try {
      const response = await fetch(`${API_URL}/api/pipeline/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message_id: message.id }),
      });

      if (!response.ok) {
        throw new Error("Failed to load pipeline preview");
      }

      const data = await response.json();
      setPipelinePreview(data);
    } catch (err) {
      console.error(err);
      setPipelinePreviewError(err.message || "Preview failed");
    } finally {
      setPipelinePreviewLoading(false);
    }
  };

  const startEditing = () => {
    setDraftSubject(currentDraft.subject);
    setDraftBody(currentDraft.body);
    setDraftError("");
    setIsEditing(true);
  };

  const handleSaveDraft = async () => {
    if (!draftBody.trim()) {
      setDraftError("Draft body cannot be empty.");
      return;
    }
    setSavingDraft(true);
    setDraftError("");
    try {
      const response = await fetch(
        `${API_URL}/api/messages/${message.id}/draft`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ subject: draftSubject, body: draftBody }),
        }
      );
      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || "Failed to save draft");
      }
      const data = await response.json();
      onMessageUpdated(message.id, data.pipeline);
      setIsEditing(false);
    } catch (err) {
      setDraftError(err.message || "Failed to save draft");
    } finally {
      setSavingDraft(false);
    }
  };

  const handleApproveDraft = async () => {
    setApproving(true);
    setDraftError("");
    try {
      const response = await fetch(
        `${API_URL}/api/messages/${message.id}/approve`,
        { method: "POST" }
      );
      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || "Failed to approve");
      }
      const data = await response.json();
      onMessageUpdated(message.id, data.pipeline);
    } catch (err) {
      setDraftError(err.message || "Failed to approve");
    } finally {
      setApproving(false);
    }
  };

  const handleSend = async () => {
    setSending(true);
    setSendError("");
    try {
      const response = await fetch(
        `${API_URL}/api/messages/${message.id}/send`,
        { method: "POST" }
      );
      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || "Failed to send");
      }
      const data = await response.json();
      onMessageUpdated(message.id, data.pipeline);
    } catch (err) {
      setSendError(err.message || "Failed to send");
    } finally {
      setSending(false);
    }
  };

  const handleChangeDecision = () => {
    setShowChangeModal(true);
  };

  return (
    <div className="detail-page">
      <button className="back-button" onClick={onBack}>
        ← Back to Attention Queue
      </button>

      <div className="detail-title-row">
        <div>
          {isPending ? (
            <span className="route route-PENDING">PENDING</span>
          ) : (
            <span className={`route route-${summary?.route}`}>
              {formatRoute(summary?.route)}
            </span>
          )}

          <h1>{message.subject}</h1>

          <p className="detail-sender">
            <strong>{message.sender_name || "Unknown sender"}</strong>
            {message.sender_address && ` · ${message.sender_address}`}
          </p>

          {message.received_at && (
            <p className="detail-meta">
              Received {new Date(message.received_at).toLocaleString()}
            </p>
          )}
        </div>

        {!isPending && (
          <div className="detail-score">
            <span>Attention score</span>
            <strong>{score}%</strong>
            <div className="detail-score-bar">
              <div style={{ width: `${score}%` }} />
            </div>
          </div>
        )}
      </div>

      <div className="detail-layout">
        <div className="detail-main-column">
          <section className="detail-card">
            <div className="detail-label">CUSTOMER MESSAGE</div>

            <p className="customer-message">{message.body_verbatim}</p>
          </section>

          {summary?.why_founder_is_needed && (
            <section className="detail-card reason-card">
              <div className="detail-label">
                WHY ATTENTION BUDDY FLAGGED THIS
              </div>

              <h3>Founder preference matched</h3>

              <p>{summary.why_founder_is_needed}</p>
            </section>
          )}

          {(summary?.draft_available || isApprovalRequired) && (
            <section className="detail-card">
              <div className="draft-header">
                <div>
                  <div className="detail-label">PREPARED RESPONSE</div>
                  <h3>{approved ? "Approved" : "Ready for your review"}</h3>
                </div>

                <div className="draft-badges">
                  {currentDraft.edited && (
                    <span className="draft-status draft-status-edited">
                      Edited by you
                    </span>
                  )}
                  <span className="draft-status">Draft</span>
                </div>
              </div>

              {isEditing ? (
                <>
                  <input
                    className="draft-subject-input"
                    value={draftSubject}
                    onChange={(e) => setDraftSubject(e.target.value)}
                    placeholder="Reply subject"
                  />
                  <textarea
                    className="draft-box"
                    value={draftBody}
                    onChange={(e) => setDraftBody(e.target.value)}
                  />
                  <div className="draft-footer">
                    <span>Review your edits before saving.</span>
                    <div className="draft-actions">
                      <button
                        className="secondary-action"
                        onClick={() => setIsEditing(false)}
                        disabled={savingDraft}
                      >
                        Cancel
                      </button>
                      <button
                        className="primary-action"
                        onClick={handleSaveDraft}
                        disabled={savingDraft}
                      >
                        {savingDraft ? "Saving…" : "Save Draft"}
                      </button>
                    </div>
                  </div>
                </>
              ) : (
                <>
                  {currentDraft.subject && (
                    <div className="draft-subject">{currentDraft.subject}</div>
                  )}
                  <p className="draft-body">
                    {currentDraft.body ||
                      "No draft yet — click Edit Draft to write one."}
                  </p>
                  <div className="draft-footer">
                    <span>Review the prepared response.</span>
                    {awaitingApproval && (
                      <button
                        className="secondary-action"
                        onClick={startEditing}
                      >
                        Edit Draft
                      </button>
                    )}
                  </div>
                </>
              )}

              {draftError && <div className="draft-error">{draftError}</div>}
            </section>
          )}
        </div>

        <aside className="decision-panel">
          {isPending ? (
            <div className="pending-state">
              <div className="detail-label">PROCESSING STATUS</div>
              <h2>Pending processing</h2>
              <p className="pending-note">
                This message is waiting to be processed by Attention Buddy.
                No decision has been made yet.
              </p>
            </div>
          ) : (
            <>
              <div className="detail-label">YOUR DECISION</div>

          <h2>
            {summary?.route === "ESCALATE_NOW"
              ? "Needs your attention now"
              : isApprovalRequired
                ? (approved ? "Approved" : "Your approval is required")
                : "Handled for you"}
          </h2>

          <div className={`comm-status comm-${communicationStatus}`}>
            {formatCommStatus(communicationStatus)}
          </div>

          {summary?.required_decisions?.length > 0 && (
            <div className="required-block">
              <span>Required</span>

              {summary.required_decisions.map((decision, index) => (
                <p key={index}>{decision}</p>
              ))}
            </div>
          )}

          {summary?.why_founder_is_needed && (
            <div className="decision-reason">
              <span>Why?</span>
              <p>{summary.why_founder_is_needed}</p>
            </div>
          )}

          {isExecuted ? (
            <div className="handled-note sent-note">✓ Sent</div>
          ) : (
            <>
              {isApprovalRequired && !approved && (
                <div className="decision-buttons">
                  <button
                    className="primary-action"
                    onClick={handleApproveDraft}
                    disabled={approving || !hasUsableDraft}
                  >
                    {approving ? "Approving…" : "Approve"}
                  </button>

                  <button
                    className="secondary-action"
                    onClick={startEditing}
                    disabled={approving}
                  >
                    Edit Draft
                  </button>

                  <button
                    className="text-action"
                    onClick={handleChangeDecision}
                    disabled={approving}
                  >
                    Change decision
                  </button>
                </div>
              )}

              {isApprovalRequired && approved && (
                <div className="handled-note">✓ Approved</div>
              )}

              {currentDecision === "ESCALATE_NOW" && !feedbackResult?.success && (
                <div className="decision-buttons">
                  <button
                    className="text-action"
                    onClick={handleChangeDecision}
                    disabled={feedbackResult?.loading}
                  >
                    Change decision
                  </button>
                </div>
              )}

              {currentDecision === "AUTO_HANDLE" &&
                !canSend &&
                !feedbackResult?.success && (
                  <div className="handled-note">
                    ✓ No action needed from you.
                  </div>
                )}

              {canSend && (
                <div className="decision-buttons send-block">
                  <button
                    className="primary-action send-action"
                    onClick={handleSend}
                    disabled={sending}
                  >
                    {sending ? "Sending…" : "Send"}
                  </button>
                </div>
              )}

              {sendError && <div className="send-error">{sendError}</div>}
            </>
          )}

          {feedbackResult?.success && (
            <div className="feedback-success">
              <CheckCircle2 size={20} />
              <div>
                <strong>Feedback recorded</strong>
                {feedbackResult?.preference_update && (
                  <p>Preference "{feedbackResult.preference_update.rule?.slice(0, 60)}…" updated.</p>
                )}
                {feedbackResult?.learning_event && (
                  <p className="learning-status">
                    Learning: {feedbackResult.learning_event.memory_status} ·{" "}
                    {feedbackResult.learning_event.confidence} confidence
                  </p>
                )}
              </div>
            </div>
          )}
            </>
          )}

          {/* ── Attention Buddy input preview (memory + business) ── */}
          <div className="pipeline-preview-section">
            <div className="detail-label">ATTENTION BUDDY INPUT PREVIEW</div>

            {!pipelinePreview && !pipelinePreviewLoading && !pipelinePreviewError && (
              <button
                className="secondary-action"
                onClick={fetchPipelinePreview}
                style={{ width: "100%", marginTop: 8 }}
              >
                Preview Attention Buddy Input
              </button>
            )}

            {pipelinePreviewLoading && (
              <div className="pipeline-loading">Loading memory and business knowledge…</div>
            )}

            {pipelinePreviewError && (
              <div className="pipeline-error">{pipelinePreviewError}</div>
            )}

            {pipelinePreview && (
              <div className="pipeline-result">
                <div className="detail-label">FOUNDER MEMORY CONTEXT</div>

                {pipelinePreview.founder_memory_context?.memory_conflict && (
                  <div className="pipeline-conflict-warning">
                    <AlertTriangle size={14} />
                    <span>Memory conflict detected</span>
                  </div>
                )}

                {pipelinePreview.founder_memory_context?.preferences?.length === 0 ? (
                  <p className="pipeline-empty">
                    No learned preferences are relevant to this message.
                  </p>
                ) : (
                  <div className="pipeline-prefs">
                    {pipelinePreview.founder_memory_context?.preferences?.map((pref) => (
                      <div key={pref.preference_id} className="pipeline-pref-item">
                        <div className="pipeline-pref-header">
                          <span className={`pref-status status-${pref.memory_status?.toLowerCase()}`}>
                            {pref.memory_status}
                          </span>
                          <span className={`pref-confidence conf-${pref.confidence?.toLowerCase()}`}>
                            {pref.confidence}
                          </span>
                          <span className="pipeline-pref-score">
                            {(pref.relevance_score * 100).toFixed(0)}%
                          </span>
                        </div>
                        <p className="pipeline-pref-rule">{pref.rule}</p>
                      </div>
                    ))}
                  </div>
                )}

                <div className="detail-label pipeline-sub-label">
                  BUSINESS KNOWLEDGE PREPARED
                </div>

                {(pipelinePreview.business_context?.knowledge || []).length === 0 ? (
                  <p className="pipeline-empty">
                    No business knowledge is relevant to this message.
                  </p>
                ) : (
                  <div className="pipeline-prefs">
                    {(pipelinePreview.business_context?.knowledge || []).map((item) => (
                      <div key={item.knowledge_id} className="pipeline-pref-item">
                        <div className="pipeline-pref-header">
                          <span className="bk-category">{formatCategory(item.category)}</span>
                          <span className="pipeline-pref-score">
                            {Math.round((item.relevance_score || 0) * 100)}%
                          </span>
                        </div>
                        <strong className="bk-title">{item.title}</strong>
                        <p className="pipeline-pref-rule">{item.content}</p>

                        {item.matched_keywords?.length > 0 && (
                          <div className="bk-matched">
                            <span>Matched on:</span>{" "}
                            {item.matched_keywords.join(" · ")}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                <button
                  className="text-action"
                  onClick={fetchPipelinePreview}
                  style={{ marginTop: 8, fontSize: 12 }}
                >
                  Refresh
                </button>
              </div>
            )}
          </div>
        </aside>
      </div>

      {showChangeModal && (
        <ChangeDecisionModal
          currentDecision={currentDecision}
          pipelineRunId={pipelineRunId}
          draftBody={draftBody}
          summary={summary}
          onClose={() => setShowChangeModal(false)}
          onSubmit={(payload) => {
            setShowChangeModal(false);
            onSubmitFeedback(payload);
          }}
        />
      )}
    </div>
  );
}

function ChangeDecisionModal({
  currentDecision,
  pipelineRunId,
  draftBody,
  summary,
  onClose,
  onSubmit,
}) {
  const [finalDecision, setFinalDecision] = useState(currentDecision);
  const [explanation, setExplanation] = useState("");
  const [applyScope, setApplyScope] = useState("one-off");

  const handleSubmit = () => {
    if (applyScope === "apply-similar" && !explanation.trim()) {
      return;
    }

    onSubmit({
      pipeline_run_id: pipelineRunId,
      original_decision: currentDecision,
      final_decision: finalDecision,
      action_type: "OVERRIDDEN",
      founder_explanation: explanation.trim() || null,
      original_draft: summary?.draft_text || null,
      final_draft: draftBody || null,
      apply_to_similar: applyScope === "apply-similar",
    });
  };

  const canSubmit =
    applyScope === "one-off" || explanation.trim().length > 0;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Change Decision</h2>
          <button className="modal-close" onClick={onClose}>
            <X size={20} />
          </button>
        </div>

        <div className="modal-body">
          <div className="form-group">
            <label>Final Decision</label>
            <select
              value={finalDecision}
              onChange={(e) => setFinalDecision(e.target.value)}
            >
              <option value="AUTO_HANDLE">AUTO_HANDLE — Handle automatically</option>
              <option value="APPROVAL_REQUIRED">
                APPROVAL_REQUIRED — Needs your approval
              </option>
              <option value="ESCALATE_NOW">
                ESCALATE_NOW — Needs immediate attention
              </option>
            </select>
          </div>

          <div className="form-group">
            <label>Explanation</label>
            <textarea
              value={explanation}
              onChange={(e) => setExplanation(e.target.value)}
              placeholder="Why are you changing this decision? If this is a pattern, describe the rule..."
              rows={4}
            />
          </div>

          <div className="form-group">
            <label>Apply this rule</label>
            <div className="radio-group">
              <label className="radio-option">
                <input
                  type="radio"
                  name="applyScope"
                  value="one-off"
                  checked={applyScope === "one-off"}
                  onChange={() => setApplyScope("one-off")}
                />
                <div>
                  <strong>One-off exception</strong>
                  <span>Just this message</span>
                </div>
              </label>

              <label className="radio-option">
                <input
                  type="radio"
                  name="applyScope"
                  value="apply-similar"
                  checked={applyScope === "apply-similar"}
                  onChange={() => setApplyScope("apply-similar")}
                />
                <div>
                  <strong>Apply this to similar messages</strong>
                  <span>Creates a learning preference (requires explanation)</span>
                </div>
              </label>
            </div>
          </div>
        </div>

        <div className="modal-footer">
          <button className="secondary-action" onClick={onClose}>
            Cancel
          </button>
          <button
            className="primary-action"
            disabled={!canSubmit}
            onClick={handleSubmit}
          >
            Save decision
          </button>
        </div>
      </div>
    </div>
  );
}

function LearningPage() {
  const [preferences, setPreferences] = useState([]);
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Memory Retrieval Preview state
  const [retrievalSubject, setRetrievalSubject] = useState("");
  const [retrievalBody, setRetrievalBody] = useState("");
  const [retrievalResults, setRetrievalResults] = useState(null);
  const [retrievalLoading, setRetrievalLoading] = useState(false);
  const [retrievalError, setRetrievalError] = useState("");

  const testRetrieval = async () => {
    if (!retrievalSubject.trim() && !retrievalBody.trim()) {
      setRetrievalError("Please enter a subject or message body.");
      return;
    }

    setRetrievalLoading(true);
    setRetrievalError("");
    setRetrievalResults(null);

    try {
      const response = await fetch(`${API_URL}/api/preferences/relevant`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          subject: retrievalSubject || null,
          body: retrievalBody || null,
          limit: 5,
        }),
      });

      if (!response.ok) {
        throw new Error("Failed to retrieve preferences");
      }

      const data = await response.json();
      setRetrievalResults(data);
    } catch (err) {
      console.error(err);
      setRetrievalError(err.message || "Retrieval failed");
    } finally {
      setRetrievalLoading(false);
    }
  };

  useEffect(() => {
    async function loadLearning() {
      try {
        const [prefsRes, eventsRes] = await Promise.all([
          fetch(`${API_URL}/api/preferences`),
          fetch(`${API_URL}/api/learning-events`),
        ]);

        if (!prefsRes.ok || !eventsRes.ok) {
          throw new Error("Failed to load learning data");
        }

        const prefsData = await prefsRes.json();
        const eventsData = await eventsRes.json();

        setPreferences(prefsData.preferences || []);
        setEvents(eventsData.events || []);
      } catch (err) {
        console.error(err);
        setError("Could not load learning data.");
      } finally {
        setLoading(false);
      }
    }

    loadLearning();
  }, []);

  if (loading) {
    return (
      <main className="main-content">
        <div className="center-state">Loading learning data...</div>
      </main>
    );
  }

  if (error) {
    return (
      <main className="main-content">
        <div className="center-state error">{error}</div>
      </main>
    );
  }

  return (
    <main className="main-content">
      <header className="topbar">
        <div>
          <p className="eyebrow">MEMORY & PREFERENCES</p>
          <h1>What Buddy has learned</h1>
          <p className="subtitle">
            Preferences and observations from your feedback.
          </p>
        </div>
      </header>

      <section className="learning-section">
        <div className="section-heading">
          <div className="section-icon learning-icon">
            <Brain size={19} />
          </div>
          <div>
            <h2>
              Learned Preferences
              <span className="count">{preferences.length}</span>
            </h2>
            <p>Reusable rules extracted from your feedback.</p>
          </div>
        </div>

        <div className="preference-grid">
          {preferences.length === 0 ? (
            <div className="empty-card">
              No learned preferences yet. As you provide feedback, patterns
              will emerge here.
            </div>
          ) : (
            preferences.map((pref) => (
              <PreferenceCard key={pref.id} preference={pref} />
            ))
          )}
        </div>
      </section>

      <section className="learning-section">
        <div className="section-heading">
          <div className="section-icon learning-events-icon">
            <Clock3 size={19} />
          </div>
          <div>
            <h2>
              Recent Learning Events
              <span className="count">{events.length}</span>
            </h2>
            <p>Every feedback action that Buddy can learn from.</p>
          </div>
        </div>

        <div className="events-list">
          {events.length === 0 ? (
            <div className="empty-card">
              No learning events recorded yet.
            </div>
          ) : (
            events.map((event) => (
              <LearningEventCard key={event.id} event={event} />
            ))
          )}
        </div>
      </section>
      <section className="learning-section">
        <div className="section-heading">
          <div className="section-icon retrieval-icon">
            <Search size={19} />
          </div>
          <div>
            <h2>
              Memory Retrieval Preview
            </h2>
            <p>
              Test which learned preferences match a hypothetical message.
            </p>
          </div>
        </div>

        <div className="retrieval-panel">
          <div className="retrieval-inputs">
            <div className="form-group">
              <label>Subject</label>
              <input
                type="text"
                value={retrievalSubject}
                onChange={(e) => setRetrievalSubject(e.target.value)}
                placeholder="e.g. Corporate bulk pricing"
                onKeyDown={(e) => e.key === "Enter" && testRetrieval()}
              />
            </div>

            <div className="form-group">
              <label>Message Body</label>
              <textarea
                value={retrievalBody}
                onChange={(e) => setRetrievalBody(e.target.value)}
                placeholder="Paste or type a customer message..."
                rows={4}
              />
            </div>

            <button
              className="primary-action retrieval-button"
              onClick={testRetrieval}
              disabled={retrievalLoading}
            >
              {retrievalLoading ? "Searching…" : "Test memory retrieval"}
            </button>
          </div>

          {retrievalError && (
            <div className="retrieval-error">{retrievalError}</div>
          )}

          {retrievalResults && (
            <div className="retrieval-results">
              {retrievalResults.memory_conflict && (
                <div className="retrieval-conflict-warning">
                  <AlertTriangle size={18} />
                  <div>
                    <strong>Memory conflict detected</strong>
                    <p>
                      Conflicting active preferences were found and excluded
                      from routing suggestions. IDs:{" "}
                      {retrievalResults.conflicting_preference_ids?.join(", ")}
                    </p>
                  </div>
                </div>
              )}

              {retrievalResults.count === 0 ? (
                <div className="retrieval-empty">
                  No learned preferences are relevant to this message.
                </div>
              ) : (
                <div className="retrieval-list">
                  {retrievalResults.preferences.map((pref) => (
                    <div key={pref.id} className="retrieval-item">
                      <div className="retrieval-item-header">
                        <span className={`pref-status status-${pref.memory_status.toLowerCase()}`}>
                          {pref.memory_status}
                        </span>
                        <span className={`pref-confidence conf-${pref.confidence.toLowerCase()}`}>
                          {pref.confidence}
                        </span>
                        <span className="retrieval-score">
                          {(pref.relevance_score * 100).toFixed(0)}% match
                        </span>
                      </div>

                      <p className="pref-rule">{pref.rule}</p>

                      <div className="pref-meta">
                        <span className="pref-scope">{pref.scope}</span>
                        <span className="pref-obs">
                          {pref.supporting_observations} observation
                          {pref.supporting_observations !== 1 ? "s" : ""}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </section>
    </main>
  );
}

function PreferenceCard({ preference }) {
  const statusClass = `status-${(preference.memory_status || "").toLowerCase()}`;
  const confClass = `conf-${(preference.confidence || "low").toLowerCase()}`;

  return (
    <article className="preference-card">
      <div className="pref-header">
        <span className={`pref-status ${statusClass}`}>
          {preference.memory_status}
        </span>
        <span className={`pref-confidence ${confClass}`}>
          {preference.confidence} confidence
        </span>
      </div>

      <p className="pref-rule">{preference.rule}</p>

      <div className="pref-meta">
        {preference.scope && (
          <span className="pref-scope">{preference.scope}</span>
        )}
        <span className="pref-obs">
          {preference.supporting_observations || 0} observation
          {(preference.supporting_observations || 0) !== 1 ? "s" : ""}
        </span>
        {preference.contradiction_count > 0 && (
          <span className="pref-contradictions">
            {preference.contradiction_count} contradiction
            {preference.contradiction_count !== 1 ? "s" : ""}
          </span>
        )}
      </div>

      {preference.last_reinforced_at && (
        <div className="pref-date">
          Last reinforced:{" "}
          {new Date(preference.last_reinforced_at).toLocaleDateString()}
        </div>
      )}
    </article>
  );
}

function LearningEventCard({ event }) {
  const payload = event.learning_event_payload || {};
  const statusClass = `status-${(event.memory_status || "").toLowerCase()}`;
  const confClass = `conf-${(event.confidence || "low").toLowerCase()}`;

  return (
    <article className="event-card">
      <div className="event-header">
        <div className="event-decision-flow">
          <span className={`route route-${payload.original_ai_decision}`}>
            {formatRoute(payload.original_ai_decision)}
          </span>
          <span className="event-arrow">→</span>
          <span className={`route route-${payload.final_founder_decision}`}>
            {formatRoute(payload.final_founder_decision)}
          </span>
        </div>

        <div className="event-badges">
          <span className={`pref-status ${statusClass}`}>
            {event.memory_status}
          </span>
          <span className={`pref-confidence ${confClass}`}>
            {event.confidence}
          </span>
        </div>
      </div>

      <div className="event-detail">
        <span className="event-action">{payload.action_type}</span>
        {payload.inferred_learning_category && (
          <span className="event-category">
            {payload.inferred_learning_category}
          </span>
        )}
      </div>

      {payload.founder_explanation && (
        <p className="event-explanation">"{payload.founder_explanation}"</p>
      )}

      {event.created_at && (
        <div className="event-date">
          {new Date(event.created_at).toLocaleDateString()}{" "}
          {new Date(event.created_at).toLocaleTimeString()}
        </div>
      )}
    </article>
  );
}

function BusinessKnowledgePage() {
  const [knowledge, setKnowledge] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    async function loadBusinessKnowledge() {
      try {
        const response = await fetch(`${API_URL}/api/business-knowledge`);

        if (!response.ok) {
          throw new Error("Failed to load business knowledge");
        }

        const data = await response.json();
        setKnowledge(data.knowledge || []);
      } catch (err) {
        console.error(err);
        setError("Could not load business knowledge.");
      } finally {
        setLoading(false);
      }
    }

    loadBusinessKnowledge();
  }, []);

  if (loading) {
    return (
      <main className="main-content">
        <div className="center-state">Loading business knowledge...</div>
      </main>
    );
  }

  if (error) {
    return (
      <main className="main-content">
        <div className="center-state error">{error}</div>
      </main>
    );
  }

  const grouped = knowledge.reduce((acc, item) => {
    const category = item.category || "OTHER";
    if (!acc[category]) acc[category] = [];
    acc[category].push(item);
    return acc;
  }, {});

  const categories = Object.keys(grouped).sort();

  return (
    <main className="main-content">
      <header className="topbar">
        <div>
          <p className="eyebrow">BUSINESS KNOWLEDGE</p>
          <h1>Business Knowledge</h1>
          <p className="subtitle">
            Facts and policies Attention Buddy uses to understand what your
            business allows.
          </p>
        </div>
      </header>

      {knowledge.length === 0 ? (
        <div className="empty-card">
          No business knowledge has been added yet. Add facts and policies
          to ground Attention Buddy in what your business actually allows.
        </div>
      ) : (
        categories.map((category) => (
          <section className="learning-section" key={category}>
            <div className="section-heading">
              <div className="section-icon bk-icon">
                <BookOpen size={19} />
              </div>
              <div>
                <h2>
                  {formatCategory(category)}
                  <span className="count">{grouped[category].length}</span>
                </h2>
                <p>Active rules for this category.</p>
              </div>
            </div>

            <div className="preference-grid">
              {grouped[category].map((item) => (
                <BusinessKnowledgeCard key={item.knowledge_id} item={item} />
              ))}
            </div>
          </section>
        ))
      )}
    </main>
  );
}

function BusinessKnowledgeCard({ item }) {
  return (
    <article className="preference-card">
      <div className="pref-header">
        <span className="bk-category">{formatCategory(item.category)}</span>
        <span className="bk-source">{item.source_type || "MANUAL"}</span>
      </div>

      <p className="pref-rule">{item.title}</p>
      <p className="bk-content">{item.content}</p>

      <div className="pref-meta">
        <span className="pref-obs">Priority {item.priority ?? "—"}</span>
      </div>

      {(item.keywords || []).length > 0 && (
        <div className="bk-keywords">
          <span className="bk-keywords-label">Keywords:</span>{" "}
          {item.keywords.join(" · ")}
        </div>
      )}
    </article>
  );
}

function formatRoute(route) {
  if (route === "ESCALATE_NOW") return "Needs you now";
  if (route === "APPROVAL_REQUIRED") return "Approval required";
  if (route === "AUTO_HANDLE") return "Handled";
  return "Unknown";
}

function formatCommStatus(status) {
  if (status === "AWAITING_APPROVAL") return "Awaiting approval";
  if (status === "READY") return "Ready";
  if (status === "HELD") return "Held";
  if (status === "PLANNED") return "Planned";
  if (status === "EXECUTED") return "Executed";
  if (status === "FAILED") return "Failed";
  return status || "Pending";
}

function formatCategory(category) {
  if (!category) return "Other";
  return String(category).replace(/_/g, " ");
}

async function submitFeedback(
  message,
  payload,
  feedbackState,
  setFeedbackState
) {
  const msgKey = message.id;

  setFeedbackState((prev) => ({
    ...prev,
    [msgKey]: { loading: true },
  }));

  try {
    const response = await fetch(`${API_URL}/api/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      const errData = await response.json().catch(() => ({}));
      throw new Error(errData.detail || "Failed to submit feedback");
    }

    const data = await response.json();

    setFeedbackState((prev) => ({
      ...prev,
      [msgKey]: {
        loading: false,
        success: true,
        data,
        preference_update: data.preference_update,
        learning_event: data.learning_event,
      },
    }));
  } catch (err) {
    console.error(err);
    setFeedbackState((prev) => ({
      ...prev,
      [msgKey]: { loading: false, success: false, error: err.message },
    }));
  }
}

export default App;

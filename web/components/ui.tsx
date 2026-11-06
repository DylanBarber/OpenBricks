"use client";

import { Children, cloneElement, isValidElement, useEffect, useId, useRef, type ReactNode } from "react";
import { Check, ChevronLeft, ChevronRight, FileCode2, LoaderCircle, X } from "lucide-react";
import { brandShapes } from "@/lib/brand";

export function BrickMark({ small = false }: { small?: boolean }) {
  return <span className={`brick-mark ${small ? "small" : ""}`} aria-hidden="true"><svg viewBox="0 0 96 96"><path d={brandShapes.body} fill="currentColor" /><path d={brandShapes.face} fill="#000" opacity=".16" /><path d={brandShapes.cap} fill={small ? "currentColor" : "#F1B788"} /></svg></span>;
}
export function Status({ status }: { status?: string }) {
  const text = status || "unknown";
  return <span className={`status status-${text.toLowerCase()}`}><span />{text.replaceAll("_", " ")}</span>;
}
export function Spinner() { return <LoaderCircle className="spin" size={16} aria-label="Loading" />; }
export function Empty({ icon = <FileCode2 size={28} />, title, children, action }: { icon?: ReactNode; title: string; children?: ReactNode; action?: ReactNode }) {
  return <div className="empty"><div className="empty-icon">{icon}</div><h3>{title}</h3><p>{children}</p>{action}</div>;
}
export function SectionHead({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description: string; actions?: ReactNode }) {
  return <div className="section-head"><div>{eyebrow ? <div className="eyebrow">{eyebrow}</div> : null}<h1>{title}</h1><p>{description}</p></div><div className="head-actions">{actions}</div></div>;
}
export function Modal({ title, children, onClose, wide = false }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => { const dialog = dialogRef.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  return <dialog ref={dialogRef} className={`modal ${wide ? "wide" : ""}`} onCancel={event => { event.preventDefault(); onClose(); }} onClick={event => { if (event.target === event.currentTarget) onClose(); }} aria-label={title}><div className="modal-head"><h2>{title}</h2><button type="button" className="icon-button" aria-label="Close dialog" onClick={onClose}><X size={18} /></button></div>{children}</dialog>;
}
export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  const labelId = useId();
  const hintId = `${labelId}-hint`;
  const controls = Children.map(children, child => {
    // Custom controls retain their own accessibility contract. Native controls
    // get the visible label rather than option text and hints inside the wrapper.
    if (!isValidElement<{
      "aria-label"?: string;
      "aria-labelledby"?: string;
      "aria-describedby"?: string;
    }>(child) || typeof child.type !== "string" || !["input", "select", "textarea"].includes(child.type)) return child;
    const props = child.props;
    const names = props["aria-label"] !== undefined || props["aria-labelledby"] !== undefined;
    const descriptions = new Set((props["aria-describedby"] || "").split(/\s+/).filter(Boolean));
    if (hint) descriptions.add(hintId);
    return cloneElement(child, {
      ...(names ? {} : { "aria-labelledby": labelId }),
      ...(descriptions.size ? { "aria-describedby": [...descriptions].join(" ") } : {}),
    });
  });
  return <label className="field"><span id={labelId}>{label}</span>{controls}{hint ? <small id={hintId}>{hint}</small> : null}</label>;
}
export function Success({ children }: { children: ReactNode }) { return <span className="success-inline"><Check size={14} />{children}</span>; }
export function Crumb({ children }: { children: ReactNode }) { return <><ChevronRight size={13} className="muted" /><span>{children}</span></>; }

export function Pagination({ total, offset, limit, onPage, busy = false }: { total: number; offset: number; limit: number; onPage: (offset: number) => void; busy?: boolean }) {
  return <div className="pagination"><span>{total ? `${offset + 1}–${Math.min(offset + limit, total)} of ${total.toLocaleString()}` : "0 results"}</span><div><button className="button" disabled={busy || offset === 0} onClick={() => onPage(Math.max(0, offset - limit))}><ChevronLeft size={14} />Previous</button><button className="button" disabled={busy || offset + limit >= total} onClick={() => onPage(offset + limit)}>Next<ChevronRight size={14} /></button></div></div>;
}

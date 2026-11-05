"use client";
import { useEffect, useRef } from "react";
import { Annotation, Compartment, EditorState } from "@codemirror/state";
import {
  EditorView,
  keymap,
  lineNumbers,
  highlightActiveLine,
  drawSelection,
} from "@codemirror/view";
import {
  defaultKeymap,
  history,
  historyKeymap,
  indentWithTab,
} from "@codemirror/commands";
import { autocompletion, completionKeymap } from "@codemirror/autocomplete";
import {
  bracketMatching,
  HighlightStyle,
  syntaxHighlighting,
  indentOnInput,
} from "@codemirror/language";
import { tags } from "@lezer/highlight";
import { searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import { python } from "@codemirror/lang-python";
import { sql } from "@codemirror/lang-sql";
import { markdown } from "@codemirror/lang-markdown";

const keywords = [
  "spark",
  "spark.sql",
  "spark.read",
  "spark.createDataFrame",
  "display",
  "params",
  "connections",
  "blob.path",
  "catalog",
  "data.quality",
  "data.read",
  "data.write",
  "data.delta.history",
  "print",
  "SELECT",
  "FROM",
  "WHERE",
  "GROUP BY",
  "ORDER BY",
  "JOIN",
];
const syntax = HighlightStyle.define([
  { tag: tags.keyword, color: "var(--syntax-keyword)" },
  {
    tag: [tags.string, tags.special(tags.string)],
    color: "var(--syntax-string)",
  },
  { tag: [tags.number, tags.bool], color: "var(--syntax-number)" },
  { tag: tags.comment, color: "var(--muted)", fontStyle: "italic" },
  {
    tag: [tags.function(tags.variableName), tags.typeName],
    color: "var(--syntax-function)",
  },
]);
const externalChange = Annotation.define<boolean>();
const theme = EditorView.theme({
  "&": {
    backgroundColor: "var(--surface)",
    color: "var(--ink)",
    fontSize: "12px",
  },
  ".cm-content": {
    fontFamily: "var(--font-mono, ui-monospace, monospace)",
    padding: "12px 0",
    minHeight: "90px",
  },
  ".cm-scroller": { overflow: "auto" },
  ".cm-gutters": {
    backgroundColor: "var(--surface-soft)",
    color: "var(--muted)",
    border: "none",
  },
  ".cm-activeLine": { backgroundColor: "var(--surface-soft)" },
  ".cm-cursor": { borderLeftColor: "var(--ink)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": {
    backgroundColor: "var(--coral-light)",
  },
  ".cm-tooltip": {
    backgroundColor: "var(--surface)",
    color: "var(--ink)",
    border: "1px solid var(--line)",
  },
  ".cm-panels": { backgroundColor: "var(--surface-soft)", color: "var(--ink)" },
  "&.cm-focused": { outline: "none" },
});
export default function CodeEditor({
  value,
  language = "python",
  onChange,
  onRun,
  disabled = false,
  label = "Code editor",
}: {
  value: string;
  language?: string;
  onChange: (value: string) => void;
  onRun?: () => void;
  disabled?: boolean;
  label?: string;
}) {
  const host = useRef<HTMLDivElement>(null),
    view = useRef<EditorView | null>(null);
  const change = useRef(onChange),
    run = useRef(onRun);
  const languageConfig = useRef(new Compartment()),
    editable = useRef(new Compartment());
  change.current = onChange;
  run.current = onRun;
  useEffect(() => {
    if (!host.current) return;
    const editor = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          lineNumbers(),
          history(),
          drawSelection(),
          highlightActiveLine(),
          highlightSelectionMatches(),
          bracketMatching(),
          indentOnInput(),
          syntaxHighlighting(syntax),
          theme,
          languageConfig.current.of(
            language === "sql"
              ? sql()
              : language === "markdown"
                ? markdown()
                : python(),
          ),
          editable.current.of([
            EditorView.editable.of(!disabled),
            EditorState.readOnly.of(disabled),
          ]),
          EditorView.contentAttributes.of({ "aria-label": label }),
          autocompletion({
            override: [
              (context) => {
                const word = context.matchBefore(/[\w.]+/);
                return word && (word.from !== word.to || context.explicit)
                  ? {
                      from: word.from,
                      options: keywords.map((label) => ({
                        label,
                        type: "keyword",
                      })),
                    }
                  : null;
              },
            ],
          }),
          keymap.of([
            {
              key: "Shift-Enter",
              run: () => {
                run.current?.();
                return !!run.current;
              },
            },
            indentWithTab,
            ...defaultKeymap,
            ...historyKeymap,
            ...completionKeymap,
            ...searchKeymap,
          ]),
          EditorView.updateListener.of((update) => {
            if (
              update.docChanged &&
              !update.transactions.some((transaction) =>
                transaction.annotation(externalChange),
              )
            )
              change.current(update.state.doc.toString());
          }),
        ],
      }),
    });
    view.current = editor;
    return () => {
      editor.destroy();
      view.current = null;
    };
  }, []);
  useEffect(() => {
    const editor = view.current;
    if (editor && value !== editor.state.doc.toString())
      editor.dispatch({
        changes: { from: 0, to: editor.state.doc.length, insert: value },
        annotations: externalChange.of(true),
      });
  }, [value]);
  useEffect(() => {
    view.current?.dispatch({
      effects: languageConfig.current.reconfigure(
        language === "sql"
          ? sql()
          : language === "markdown"
            ? markdown()
            : python(),
      ),
    });
  }, [language]);
  useEffect(() => {
    view.current?.dispatch({
      effects: editable.current.reconfigure([
        EditorView.editable.of(!disabled),
        EditorState.readOnly.of(disabled),
      ]),
    });
  }, [disabled]);
  return <div ref={host} className="code-editor" />;
}

import { useThemedStyles } from "../../shared/theme/ThemeProvider";
import React, { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { presentChatResults } from "./chatResultPresentation.cjs";

const citationLabel = indices => `근거 ${indices.map(index => index + 1).join(", ")}`;

function ExpandableRows({ rows, renderRow }) {
  const p = useThemedStyles(createStyles);
  const [expanded, setExpanded] = useState(false);
  return <View style={p.rows}>
    {(expanded ? rows : rows.slice(0, 4)).map(renderRow)}
    {rows.length > 4 ? <Pressable accessibilityRole="button" accessibilityState={{ expanded }} onPress={() => setExpanded(value => !value)} style={p.expand}>
      <Text style={p.expandText}>{expanded ? "접기" : `${rows.length - 4}개 더 보기`}</Text>
    </Pressable> : null}
  </View>;
}

function When({ when, label }) {
  const p = useThemedStyles(createStyles);
  return <View style={p.when}>
    {label ? <Text style={p.meta}>{label}</Text> : null}
    <Text style={[p.date, !when.known && p.unknown]}>{when.text}</Text>
    {when.known ? <Text style={p.meta}>{when.precision}</Text> : null}
  </View>;
}

function StructuredCard({ result }) {
  const p = useThemedStyles(createStyles);
  return <View style={p.card}>
    {result.title ? <Text accessibilityRole="header" style={p.heading}>{result.title}</Text> : null}
    {result.notice ? <Text style={p.notice}>{result.notice}</Text> : null}
    {result.count !== null ? <View style={p.countBox}><Text style={p.count}>{result.count}회</Text><Text style={p.meta}>열람 가능한 검토된 사건 기준</Text></View> : null}
    {result.groups.map(group => <View key={group.key} style={p.summaryRow}><Text style={p.body}>{group.label}</Text><Text style={p.summaryValue}>{group.count}회</Text></View>)}
    {result.milestones.length ? <View style={p.rows}>{result.milestones.map(value => <When key={value.label} label={value.label} when={value.when} />)}</View> : null}
    {result.entries.length ? <ExpandableRows rows={result.entries} renderRow={entry => <View key={entry.key} style={p.entry}>
      <When when={entry.when} />
      <Text selectable style={p.title}>{entry.title}</Text>
      <Text style={p.meta}>{[entry.type, entry.state, entry.location].filter(Boolean).join(" · ")}</Text>
      {entry.participants.map((label, index) => <Text key={index} style={p.meta}>{label}</Text>)}
      <Text style={p.source}>{citationLabel(entry.citationIndices)}</Text>
    </View>} /> : null}
  </View>;
}

function ClaimCard({ result }) {
  const p = useThemedStyles(createStyles);
  return <View style={p.card}>
    <Text accessibilityRole="header" style={p.heading}>기록별 관점과 확인 범위</Text>
    {result.notices.map((notice, index) => <Text key={index} style={p.notice}>{notice}</Text>)}
    {result.perspectives.length ? <ExpandableRows rows={result.perspectives} renderRow={value => <View key={value.key} style={p.entry}>
      <Text style={p.title}>{value.title}</Text>
      <Text style={p.badge}>{value.basis}</Text>
      {value.memory ? <Text style={p.body}>기억 상태 · {value.memory}</Text> : null}
      {value.dates.map(date => <When key={date.label} label={date.label} when={date.when} />)}
      <Text style={p.meta}>화자와 실제 발언은 아래 원본 근거에서 확인하세요.</Text>
    </View>} /> : null}
    {result.participation.length ? <>
      <Text style={p.subheading}>참여 기록</Text>
      <ExpandableRows rows={result.participation} renderRow={value => <View key={value.key} style={p.entry}>
        <View style={p.summaryRow}><Text style={p.title}>{value.label}</Text><Text style={p.badge}>{value.mode}</Text></View>
        {value.when ? <When when={value.when} /> : null}
        <Text style={p.source}>{citationLabel(value.citationIndices)}</Text>
      </View>} />
    </> : null}
  </View>;
}

export function ChatResultCards({ response }) {
  const p = useThemedStyles(createStyles);
  const result = presentChatResults(response);
  if (!result.structured && !result.claims) return null;
  return <View style={p.results}>
    {result.structured ? <StructuredCard result={result.structured} /> : null}
    {result.claims ? <ClaimCard result={result.claims} /> : null}
  </View>;
}

const createStyles = colors => StyleSheet.create({
  results: { gap: 10 },
  card: { padding: 16, borderRadius: 16, borderWidth: 1, borderColor: colors.border, backgroundColor: colors.surface, gap: 12 },
  heading: { fontSize: 15, fontWeight: "700", color: colors.text, lineHeight: 22 },
  subheading: { fontSize: 13, fontWeight: "700", color: colors.text, marginTop: 4 },
  rows: { gap: 12 },
  entry: { borderLeftWidth: 3, borderLeftColor: colors.primarySoft, paddingLeft: 12, gap: 5 },
  title: { fontSize: 14, fontWeight: "600", color: colors.text, lineHeight: 21, flexShrink: 1 },
  body: { fontSize: 13, lineHeight: 20, color: colors.text, flexShrink: 1 },
  meta: { fontSize: 12, lineHeight: 18, color: colors.muted },
  when: { gap: 2 },
  date: { fontSize: 13, lineHeight: 20, fontWeight: "600", color: colors.primaryStrong },
  unknown: { color: colors.muted, fontWeight: "400" },
  notice: { fontSize: 13, lineHeight: 21, color: colors.primaryStrong, backgroundColor: colors.primarySoft, borderRadius: 10, padding: 10 },
  countBox: { padding: 12, borderRadius: 12, gap: 4, backgroundColor: colors.primarySoft },
  count: { fontSize: 26, lineHeight: 34, fontWeight: "700", color: colors.primaryStrong },
  summaryRow: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 8 },
  summaryValue: { fontSize: 15, fontWeight: "700", color: colors.primaryStrong },
  badge: { alignSelf: "flex-start", fontSize: 12, lineHeight: 18, color: colors.primaryStrong, backgroundColor: colors.primarySoft, borderRadius: 6, paddingVertical: 3, paddingHorizontal: 7 },
  source: { fontSize: 12, lineHeight: 18, color: colors.primaryStrong },
  expand: { minHeight: 44, paddingHorizontal: 10, alignItems: "center", justifyContent: "center", borderRadius: 10, backgroundColor: colors.primarySoft },
  expandText: { fontSize: 13, fontWeight: "600", color: colors.primaryStrong },
});

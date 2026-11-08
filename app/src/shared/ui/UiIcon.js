import { useTheme, useThemedStyles } from "../theme/ThemeProvider";
import React from "react";
import { View, StyleSheet } from "react-native";
export function UiIcon({
  name,
  color: suppliedColor,
  size = 24
}) {
  const { colors } = useTheme();
  const i = useThemedStyles(createIconStyles);
  const color = suppliedColor ?? colors.primary;
  const stroke = {
    borderColor: color
  };
  const line = {
    backgroundColor: color
  };
  let drawing;
  if (name === "home") drawing = <>
    <View style={[i.roofLeft, line]} /><View style={[i.roofRight, line]} />
    <View style={[i.house, stroke]} /><View style={[i.door, stroke]} />
  </>;else if (name === "records") drawing = <>
    <View style={[i.stackLine, line]} /><View style={[i.stackTop, line]} />
    <View style={[i.album, stroke]} />
  </>;else if (name === "chat") drawing = <>
    <View style={[i.backBubble, stroke]} /><View style={[i.frontBubble, stroke]} />
    <View style={[i.bubbleTail, line]} />
  </>;else if (name === "person") drawing = <>
    <View style={[i.head, stroke]} /><View style={[i.shoulders, stroke]} />
  </>;else if (name === "image") drawing = <>
    <View style={[i.imageFrame, stroke]} /><View style={[i.imageSun, line]} />
    <View style={[i.mountainLeft, line]} /><View style={[i.mountainRight, line]} />
  </>;else if (name === "video") drawing = <>
    <View style={[i.videoFrame, stroke]} />
    <View style={[i.playTriangle, {
      borderLeftColor: color
    }]} />
  </>;else if (name === "voice") drawing = <>
    <View style={[i.microphone, stroke]} /><View style={[i.microphoneArc, stroke]} />
    <View style={[i.microphoneStem, line]} /><View style={[i.microphoneBase, line]} />
  </>;else if (name === "text") drawing = <>
    <View style={[i.document, stroke]} />
    {[8, 12, 16].map(top => <View key={top} style={[i.documentLine, line, {
      top
    }]} />)}
  </>;else if (name === "close") drawing = <>
    <View style={[i.middleLine, line, {
      transform: [{
        rotate: "45deg"
      }]
    }]} />
    <View style={[i.middleLine, line, {
      transform: [{
        rotate: "-45deg"
      }]
    }]} />
  </>;else if (name === "chevron") drawing = <>
    <View style={[i.chevronTop, line]} /><View style={[i.chevronBottom, line]} />
  </>;else if (name === "down") drawing = <>
    <View style={[i.downLeft, line]} /><View style={[i.downRight, line]} />
  </>;else if (name === "send") drawing = <>
    <View style={[i.sendStem, line]} /><View style={[i.sendLeft, line]} /><View style={[i.sendRight, line]} />
  </>;else drawing = <>
    <View style={[i.middleLine, line]} />
    <View style={[i.middleLine, line, {
      transform: [{
        rotate: "90deg"
      }]
    }]} />
  </>;
  return <View accessible={false} importantForAccessibility="no" style={{
    width: size,
    height: size,
    alignItems: "center",
    justifyContent: "center"
  }}>
    <View style={[i.canvas, {
      transform: [{
        scale: size / 24
      }]
    }]}>{drawing}</View>
  </View>;
}
const createIconStyles = colors => StyleSheet.create({
  canvas: {
    width: 24,
    height: 24
  },
  roofLeft: {
    position: "absolute",
    width: 13,
    height: 2,
    top: 6,
    left: 1,
    transform: [{
      rotate: "-42deg"
    }]
  },
  roofRight: {
    position: "absolute",
    width: 13,
    height: 2,
    top: 6,
    right: 1,
    transform: [{
      rotate: "42deg"
    }]
  },
  house: {
    position: "absolute",
    width: 16,
    height: 14,
    top: 8,
    left: 4,
    borderWidth: 1.6,
    borderTopWidth: 0,
    borderRadius: 1
  },
  door: {
    position: "absolute",
    width: 5,
    height: 8,
    bottom: 2,
    left: 9.5,
    borderWidth: 1.5,
    borderBottomWidth: 0
  },
  stackLine: {
    position: "absolute",
    width: 13,
    height: 1.5,
    top: 2,
    left: 5.5,
    borderRadius: 1
  },
  stackTop: {
    position: "absolute",
    width: 17,
    height: 1.5,
    top: 5,
    left: 3.5,
    borderRadius: 1
  },
  album: {
    position: "absolute",
    width: 20,
    height: 14,
    top: 8,
    left: 2,
    borderWidth: 1.5,
    borderRadius: 2
  },
  backBubble: {
    position: "absolute",
    width: 15,
    height: 15,
    top: 7,
    right: 1,
    borderWidth: 1.5,
    borderRadius: 7
  },
  frontBubble: {
    position: "absolute",
    width: 17,
    height: 17,
    top: 2,
    left: 1,
    borderWidth: 1.5,
    borderRadius: 8,
    backgroundColor: colors.surface
  },
  bubbleTail: {
    position: "absolute",
    width: 6,
    height: 1.5,
    bottom: 3.5,
    left: 1,
    transform: [{
      rotate: "-48deg"
    }]
  },
  head: {
    position: "absolute",
    width: 9,
    height: 9,
    borderWidth: 1.5,
    borderRadius: 5,
    top: 1,
    left: 7.5
  },
  shoulders: {
    position: "absolute",
    width: 18,
    height: 11,
    borderWidth: 1.5,
    borderTopLeftRadius: 10,
    borderTopRightRadius: 10,
    borderBottomLeftRadius: 2,
    borderBottomRightRadius: 2,
    top: 12,
    left: 3
  },
  imageFrame: {
    position: "absolute",
    width: 22,
    height: 18,
    borderWidth: 1.5,
    borderRadius: 3,
    top: 3,
    left: 1
  },
  imageSun: {
    position: "absolute",
    width: 3.5,
    height: 3.5,
    borderRadius: 2,
    top: 7,
    right: 5
  },
  mountainLeft: {
    position: "absolute",
    width: 9,
    height: 1.5,
    top: 14,
    left: 3,
    transform: [{
      rotate: "-45deg"
    }]
  },
  mountainRight: {
    position: "absolute",
    width: 12,
    height: 1.5,
    top: 15,
    left: 8,
    transform: [{
      rotate: "45deg"
    }]
  },
  videoFrame: {
    position: "absolute",
    width: 22,
    height: 18,
    borderWidth: 1.5,
    borderRadius: 3,
    top: 3,
    left: 1
  },
  playTriangle: {
    position: "absolute",
    width: 0,
    height: 0,
    left: 10,
    top: 7,
    borderTopWidth: 5,
    borderBottomWidth: 5,
    borderLeftWidth: 7,
    borderTopColor: "transparent",
    borderBottomColor: "transparent"
  },
  microphone: {
    position: "absolute",
    width: 8,
    height: 13,
    borderWidth: 1.5,
    borderRadius: 5,
    top: 1,
    left: 8
  },
  microphoneArc: {
    position: "absolute",
    width: 14,
    height: 12,
    borderWidth: 1.5,
    borderTopWidth: 0,
    borderBottomLeftRadius: 8,
    borderBottomRightRadius: 8,
    top: 6,
    left: 5
  },
  microphoneStem: {
    position: "absolute",
    width: 1.5,
    height: 5,
    top: 17,
    left: 11.25
  },
  microphoneBase: {
    position: "absolute",
    width: 9,
    height: 1.5,
    bottom: 1,
    left: 7.5
  },
  document: {
    position: "absolute",
    width: 17,
    height: 22,
    borderWidth: 1.5,
    borderRadius: 2,
    top: 1,
    left: 3.5
  },
  documentLine: {
    position: "absolute",
    width: 10,
    height: 1.5,
    left: 7
  },
  middleLine: {
    position: "absolute",
    width: 18,
    height: 1.5,
    top: 11.25,
    left: 3
  },
  chevronTop: {
    position: "absolute",
    width: 9,
    height: 1.5,
    top: 8,
    left: 8,
    transform: [{
      rotate: "45deg"
    }]
  },
  chevronBottom: {
    position: "absolute",
    width: 9,
    height: 1.5,
    top: 14,
    left: 8,
    transform: [{
      rotate: "-45deg"
    }]
  },
  downLeft: {
    position: "absolute",
    width: 10,
    height: 1.5,
    top: 12,
    left: 4,
    transform: [{
      rotate: "45deg"
    }]
  },
  downRight: {
    position: "absolute",
    width: 10,
    height: 1.5,
    top: 12,
    right: 4,
    transform: [{
      rotate: "-45deg"
    }]
  },
  sendStem: {
    position: "absolute",
    width: 1.5,
    height: 17,
    top: 4,
    left: 11.25
  },
  sendLeft: {
    position: "absolute",
    width: 10,
    height: 1.5,
    top: 7,
    left: 4,
    transform: [{
      rotate: "-45deg"
    }]
  },
  sendRight: {
    position: "absolute",
    width: 10,
    height: 1.5,
    top: 7,
    right: 4,
    transform: [{
      rotate: "45deg"
    }]
  }
});

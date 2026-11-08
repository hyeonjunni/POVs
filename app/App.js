import "react-native-gesture-handler";
import React from "react";
import AppNavigator from "./src/navigation/AppNavigator";
import { ThemeProvider } from "./src/shared/theme/ThemeProvider";

export default function App() {
  return <ThemeProvider><AppNavigator /></ThemeProvider>;
}

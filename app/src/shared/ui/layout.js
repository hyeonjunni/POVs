import { Platform, Keyboard, Dimensions, useWindowDimensions } from "react-native";
import { useState, useEffect } from "react";
export const KEYBOARD_AVOIDING_BEHAVIOR = Platform.OS === "ios" ? "padding" : "height";
export const BASE_TAB_BAR_BOTTOM_PADDING = 22;
export const BASE_SCROLL_BOTTOM_PADDING = 112;
export const COMMON_SINGLE_LINE_INPUT_PROPS = {
  returnKeyType: "done",
  blurOnSubmit: true,
  onSubmitEditing: Keyboard.dismiss
};
export function getAndroidBottomInset(windowHeight) {
  if (Platform.OS !== "android") {
    return 0;
  }
  const screenHeight = Dimensions.get("screen").height;
  const measuredInset = Math.max(0, Math.round(screenHeight - windowHeight));
  return Math.max(measuredInset, 28);
}
export function useAndroidBottomInset() {
  const {
    height: windowHeight
  } = useWindowDimensions();
  const [bottomInset, setBottomInset] = useState(() => getAndroidBottomInset(windowHeight));
  useEffect(() => {
    setBottomInset(getAndroidBottomInset(windowHeight));
  }, [windowHeight]);
  return bottomInset;
}
export function useKeyboardInset() {
  const [keyboardInset, setKeyboardInset] = useState(0);
  useEffect(() => {
    const showEvent = Platform.OS === "ios" ? "keyboardWillShow" : "keyboardDidShow";
    const hideEvent = Platform.OS === "ios" ? "keyboardWillHide" : "keyboardDidHide";
    const showSubscription = Keyboard.addListener(showEvent, event => {
      setKeyboardInset(event.endCoordinates?.height || 0);
    });
    const hideSubscription = Keyboard.addListener(hideEvent, () => {
      setKeyboardInset(0);
    });
    return () => {
      showSubscription.remove();
      hideSubscription.remove();
    };
  }, []);
  return keyboardInset;
}

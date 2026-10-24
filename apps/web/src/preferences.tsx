import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import * as api from "./api";

/**
 * 前回選んだ生成方法・枚数を、画面の初期表示に使う。
 *
 * 「動画はH3で作る」「候補は12枚」のような選択は、一度決めたら作品をまたいで同じものを
 * 使い続けることがほとんどで、毎回選び直させる理由が無い。値はAPI側（この端末のDB）に
 * 置き、どの画面からも同じものを読む。
 *
 * 選び直したときは、その場で画面へ反映してから保存する。保存に失敗しても操作は止めない
 * ——「次回の既定値を覚える」は補助の機能で、いま作っているものより優先させない。
 */

const DEFAULTS: api.Preferences = {
  composition_quality: "fast",
  video_engine: "wan22",
  candidate_count: 8,
  variation_strength: 3.0,
  candidate_history: "discard",
  recommendation_tier: "",
  updated_at: null,
};

interface PreferencesValue {
  preferences: api.Preferences;
  /** 読み込みが終わったか。終わるまでは既定値が入っている。 */
  loaded: boolean;
  /** 選び直した値を覚える（画面には即座に反映する）。 */
  update: (patch: Partial<api.Preferences>) => void;
  /** おすすめの適用など、外から変わったときに読み直す。 */
  reload: () => Promise<void>;
}

const PreferencesContext = createContext<PreferencesValue>({
  preferences: DEFAULTS,
  loaded: false,
  update: () => {},
  reload: async () => {},
});

export function PreferencesProvider({ children }: { children: ReactNode }) {
  const [preferences, setPreferences] = useState<api.Preferences>(DEFAULTS);
  const [loaded, setLoaded] = useState(false);

  const reload = useCallback(async () => {
    try {
      setPreferences(await api.getPreferences());
    } catch {
      // 読めなくても既定値で操作はできる。ここで画面を止めない。
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const update = useCallback((patch: Partial<api.Preferences>) => {
    setPreferences((current) => ({ ...current, ...patch }));
    void api.putPreferences(patch).catch(() => {
      // 覚え損ねても、いまの選択は画面に残す。
    });
  }, []);

  const value = useMemo(
    () => ({ preferences, loaded, update, reload }),
    [preferences, loaded, update, reload],
  );
  return <PreferencesContext.Provider value={value}>{children}</PreferencesContext.Provider>;
}

export function usePreferences(): PreferencesValue {
  return useContext(PreferencesContext);
}

/**
 * 1項目ぶんを、useState と同じ形で扱う。
 *
 * 読み込みが終わるまでは既定値が入っているため、終わった時点で覚えていた値へ差し替える
 * （最初の描画のためだけに、画面全体を待たせない）。
 */
export function useRememberedSetting<K extends keyof api.Preferences>(
  key: K,
): [api.Preferences[K], (value: api.Preferences[K]) => void] {
  const { preferences, update } = usePreferences();
  const setValue = useCallback(
    (value: api.Preferences[K]) => update({ [key]: value } as Partial<api.Preferences>),
    [key, update],
  );
  return [preferences[key], setValue];
}

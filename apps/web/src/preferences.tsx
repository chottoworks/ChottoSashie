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
  candidate_precision: "bf16",
  variation_strength: 3.0,
  candidate_history: "discard",
  recommendation_tier: "",
  recommended: {},
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

/** 選んだ選択肢のモデルが揃っていないときの注意。揃っていれば null。 */
export function missingModelNote(status: api.ModelChoiceStatus, label: string): string | null {
  if (status.available) return null;
  return status.can_download
    ? `「${label}」のモデルをまだ取得していません。セットアップの「モデルの取得」から取得してください。`
    : `「${label}」のモデルが接続先のComfyUIにありません。既存ComfyUIのmodelsフォルダへ配置してください。`;
}

/**
 * 使うモデルが変わる設定で、選んでいる選択肢のモデルが揃っていないときの注意。
 *
 * モデルは選択肢ごとに取得するので、選んだ選択肢のモデルが無いことがある。生成して初めて
 * 気づくことのないよう、選んだ時点で実際の有無を確かめて伝える。確かめられないとき
 * （ComfyUIに接続できないなど）は、無いと言い切れないので何も出さない。
 */
export function useMissingModelNote<K extends keyof api.Preferences>(
  key: K,
  labels: Record<string, string>,
): string | null {
  const { preferences, loaded } = usePreferences();
  const value = String(preferences[key]);
  const [status, setStatus] = useState<api.ModelChoiceStatus | null>(null);

  useEffect(() => {
    if (!loaded) return;
    let stale = false;
    setStatus(null);
    api.getModelChoiceStatus(key, value).then(
      (next) => {
        if (!stale) setStatus(next);
      },
      () => undefined,
    );
    return () => {
      stale = true;
    };
  }, [key, value, loaded]);

  return status && missingModelNote(status, labels[value] ?? value);
}

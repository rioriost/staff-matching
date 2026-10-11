import { useEffect, useRef, useState } from "react";
import type { Map as LeafletMap } from "leaflet";
import { saasApi } from "../saas-api.ts";
import type { Area, Commute, Site } from "../saas-models.ts";
import { LoadState, useResource } from "./shared.tsx";
import Icon from "../Icon.tsx";
import "leaflet/dist/leaflet.css";

export default function PublicMap({ site, area, commute }: { site?: Site; area?: Area; commute?: Commute }) {
  const element = useRef<HTMLDivElement>(null);
  const map = useRef<LeafletMap | null>(null);
  const reset = useRef<() => void>(() => {});
  const [mapError, setMapError] = useState("");
  const basemap = useResource("public-basemap", saasApi.basemap);
  useEffect(() => {
    if (!element.current || !basemap.data) return;
    const container = element.current;
    let disposed = false;
    let observer: ResizeObserver | undefined;
    void import("leaflet").then((L) => {
      if (disposed) return;
      setMapError("");
      const current = L.map(container, {
        scrollWheelZoom: false, zoomControl: false, attributionControl: false,
        preferCanvas: false, fadeAnimation: false, zoomAnimation: false, markerZoomAnimation: false,
      }).setView([35.68, 139.75], 9, { animate: false });
      map.current = current;
      L.control.zoom({ zoomInTitle: "地図を拡大", zoomOutTitle: "地図を縮小" }).addTo(current);
      L.control.attribution({ prefix: false }).addAttribution(
        '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap contributors</a>',
      ).addTo(current);
      L.geoJSON(basemap.data, {
        style: { color: "#718da6", weight: 1.4, fillColor: "#d7e6ef", fillOpacity: 0.25 },
        pointToLayer: (_feature, point) => L.circleMarker(point, { radius: 2, color: "#7f91a4", weight: 1 }),
      }).addTo(current);
      const bounds = L.latLngBounds([]);
      if (site) {
        const location = L.latLng(site.point.lat, site.point.lon);
        bounds.extend(location);
        L.circleMarker(location, { radius: 9, color: "#174da1", fillColor: "#236cce", fillOpacity: 1, weight: 3 }).addTo(current);
      }
      if (area) {
        const representative = L.latLng(area.point.lat, area.point.lon);
        bounds.extend(representative);
        L.circleMarker(representative, { radius: 12, color: "#97620e", fillColor: "#e4b84d", fillOpacity: .6, weight: 2, dashArray: "4 4" }).addTo(current);
      }
      // This path is public network geometry only. Never join the area point to a home endpoint.
      if (commute && commute.public_path.length > 1) {
        const path = commute.public_path.map((point) => L.latLng(point.lat, point.lon));
        path.forEach((point) => bounds.extend(point));
        L.polyline(path, { color: "#8052c3", weight: 4, dashArray: "7 6" }).addTo(current);
      }
      reset.current = () => {
        current.invalidateSize();
        if (bounds.isValid()) current.fitBounds(bounds, { padding: [35, 35], maxZoom: 13, animate: false });
        else current.setView([35.68, 139.75], 9, { animate: false });
      };
      reset.current();
      observer = new ResizeObserver(() => {
        if (container.clientWidth && container.clientHeight) reset.current();
      });
      observer.observe(container);
    }).catch(() => {
      if (!disposed) {
        setMapError("地図を表示できません。以下の所在地・駅・所要時間をご確認ください。");
      }
    });
    return () => {
      disposed = true;
      observer?.disconnect();
      map.current?.remove();
      map.current = null;
      reset.current = () => {};
    };
  }, [site, area, commute, basemap.data]);
  return <section className="saas-map-section" aria-label="公開位置と通勤の概算">
    <div className="saas-section-title"><h4 className="ui-heading"><Icon name="location" />所在地と通勤</h4><button onClick={() => reset.current()}><Icon name="refresh" />表示範囲を戻す</button></div>
    <LoadState resource={basemap} />
    {mapError && <p role="status">{mapError}</p>}
    <div ref={element} className="saas-map" aria-label="公開就業先と居住エリアの代表点。矢印キーで移動できます。" />
    <p className="saas-hint">● 青：公開就業先　◌ 黄：居住エリア代表点　破線：公開経路のみ。自宅の位置や自宅側の経路は表示しません。</p>
    <ul className="saas-location-list">
      {site && <li><strong>{site.name}</strong> — {site.address}<br />
        {site.stations.length ? site.stations.map((s) => `${s.name} 徒歩${s.walking_minutes}分`).join(" ／ ") : "最寄駅情報なし"}<br />
        駐車場：{site.parking ? "あり" : "なし"} ／ {site.accessibility}
      </li>}
      {area && <li>居住エリア：{area.label}（代表点であり、自宅ではありません）</li>}
      {commute && <li><strong>{commute.status === "estimated" && commute.duration_seconds !== null
        ? `概算 ${Math.ceil(commute.duration_seconds / 60)}分（${commute.mode === "car" ? "自動車" : "公共交通"}）`
        : commute.status === "unreachable" ? "対象ネットワークで経路を確認できません" : "経路・所要時間は不明"}</strong>
        <br />{commute.summary}
        {commute.station_names.length > 0 && <><br />経由駅：{commute.station_names.join(" → ")}</>}
        <br />{commute.uncertainty}
        {commute.public_path.length === 0 && <><br />公開経路の描画データはありません。</>}
      </li>}
    </ul>
    <p className="saas-hint">公開ベクトル地図・経路は対象地域の概略です。実際の時刻表や当日の運行は確認してください。<br />
      © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap contributors</a>
      {" · "}<a href="/api/saas/geo/manifest" target="_blank" rel="noreferrer">地図データの出典・ライセンスを開く／保存</a>
    </p>
  </section>;
}

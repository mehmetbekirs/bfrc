# BFRC grafik paneli — kurulum ve başlangıç kodu

Hazırlayan: Mehmet Faruk Bekir için teknik başlangıç paketi · 12 Ekim 2026

## Seçilen yapı

Python, GitHub Actions üzerinde API verilerini çeker. Plotly ile etkileşimli HTML grafiklerini üretir. GitHub Pages bu dosyaları HTTPS ile sunar. BFRC WordPress sayfası paneli iframe içinde gösterir.

WordPress paneli Python çalıştırmaz. WordPress sunucusuna SSH, FTP, eklenti kurma veya cron erişimi gerekmez. GitHub hesabı/repository yönetim erişimi ve veri sağlayıcısının API anahtarı gerekir. Ücretsiz başlangıç için repository public seçilir; GitHub Pages ücretsiz planda public repository destekler. Özel kaynak kod istenirse uygun GitHub planı veya ayrı barındırma değerlendirilir.

Bu paket kuruluma hazır başlangıç kodudur. Henüz bir GitHub hesabında yayımlanmış veya WordPress'e eklenmiş değildir. Resmi BFRC logo dosyası ve gerçek API anahtarları bu pakete dahil değildir.

| Katman | Görevi | Erişim |
|---|---|---|
| FRED / TCMB | Zaman serisi verisi | Python üzerinden API |
| GitHub Actions | Veri çekme, doğrulama, grafik üretme | Repository yöneticisi |
| state/cache.json | Son başarılı gözlemler | Otomatik repository commit'i |
| GitHub Pages | HTML ve Plotly JavaScript yayını | Public HTTPS |
| WordPress | Paneli mevcut sayfada gösterme | Özel HTML bloğu / mevcut HTML widget'ı |

Bu tasarım günlük/makro göstergeler içindir. Canlı terminal fiyatı veya saniyelik güncelleme sağlamaz. Ziyaretçi grafiği etkileşimli kullanır; sunucu Python işlemi yalnızca güncelleme görevinde çalışır. Böylece ziyaretçi sayısı API çağrı sayısını artırmaz.

## Paket içeriği

- build.py: FRED bağlantısı, devre dışı EVDS2 başlangıç adaptörü, veri kontrolü, önbellek ve grafik üretimi.
- config.json: Gösterge ve grafik listesi; başlık, birim, kaynak ve gecikme eşiği.
- .github/workflows/update.yml: Zamanlanmış API yenileme ve Pages yayını.
- requirements.txt: Sabitlenmiş Python bağımlılıkları.
- wordpress-iframe.html: WordPress'e konacak iframe.
- assets/README.md: Logo ekleme talimatı.
- tests/test_data.py: Eksik değerler, tarih kontrolü, FRED ayrıştırma, faiz farkı ve anahtar sızıntısını kontrol eden testler.
- demo/index.html: Kurulum öncesi tasarım kontrolü; yalnızca sentetik veri. Canlı diye yayımlamayın.

## 1. İlk grafikler ve veri kaynakları

| Grafik | Seri | Durum |
|---|---|---|
| ABD 2 ve 10 yıl tahvil getirileri | FRED DGS2, DGS10 | API anahtarı eklenince etkin |
| ABD 10 yıl − 2 yıl farkı | Aynı tarihli DGS10 − DGS2 × 100 | Baz puan olarak hesaplanır |
| USD/TRY satış kuru | EVDS TP.DK.USD.S.YTL | EVDS bağlantısı doğrulanana kadar kapalı |

FRED bir dağıtım platformudur; DGS2 ve DGS10 için özgün kaynak ABD Hazine Bakanlığıdır. Başlık, birim ve kaynak grafiğin içine yazılır. Seriler kaynak güncelleme takvimine göre gecikmeli olabilir. API'yi kontrol etmek yeni gözlem yayımlandığı anlamına gelmez.

TCMB'nin eski EVDS doküman bağlantısı araştırma sırasında EVDS3'e yönlenmiştir. Bu nedenle evds_legacy sağlayıcısı EVDS2 sözleşmesiyle yazılmış, **varsayılan olarak kapalı bırakılmıştır**; doğrulanmış EVDS3 adaptörü değildir. Genel bir hostname değiştirmenin yeterli olduğu varsayılmamalıdır. EVDS hesabından güncel web servis dokümanını alın; endpoint, anahtar gönderim biçimi, yanıt alanları ve seri kodunu kontrol edin. Adaptör yalnızca günlük gg-aa-yyyy tarih biçimini kabul eder; aylık/çeyreklik EVDS serileri için ayrı tarih normalizasyonu gerekir. Canlı örnek JSON ile eşleştirmeden enabled=true yapmayın.

Türkiye TÜFE, sanayi üretimi, rezervler ve politika faizi sonraki genişletme adımıdır. Her seri için güncel resmi seri kodu, mevsim/takvim arındırması, birim ve sıklık doğrulanmalıdır. Bu pakette doğrulanmamış kod veya TÜİK API endpoint'i uydurulmamıştır. Endeks düzeyi, aylık değişim ve yıllık değişim ayrı grafikler olarak tanımlanmalıdır; günlük faiz serisi ayarını aylık veriye kopyalamayın.

## 2. GitHub üzerinden kurulum

1. Kurumun kontrolündeki GitHub hesabı veya organizasyonunda **bfrc-panel** adlı public repository açın. Varsayılan dal main olsun. Kişisel hesaba bağlı tek yönetici yerine kurumdan ikinci yönetici eklemek devamlılığı sağlar.
2. ZIP'i açın. **bfrc-panel klasörünün içindeki dosyaları** repository köküne yükleyin; fazladan bfrc-panel/bfrc-panel klasörü oluşmasın. build.py ve requirements.txt kökte olmalıdır.
3. .github/workflows/update.yml dosyasının gerçekten yüklendiğini kontrol edin. Dosya gezgini noktayla başlayan klasörü göstermiyorsa GitHub'da Add file → Create new file ile tam bu yolu oluşturup YAML içeriğini yapıştırın. assets klasörünü de yükleyin. demo klasörünü yüklemek gerekli değildir.
4. FRED hesabınızdan API anahtarı oluşturun: https://fred.stlouisfed.org/docs/api/api_key.html
5. Repository → Settings → Secrets and variables → Actions → New repository secret. İsim: **FRED_API_KEY**. Değer: gerçek anahtar. Anahtarı config.json, build.py, README veya WordPress içine koymayın.
6. Repository → Settings → Pages → Build and deployment → Source: **GitHub Actions**.
7. Repository → Settings → Actions → General üzerinden Actions'ın kullanılabildiğini ve otomatik cache commit'inin contents: write iznine sahip olabildiğini kontrol edin. Kurum politikası veya main dal koruması doğrudan bot push'unu engelliyorsa kurum yöneticisinin bir state dalı/depolama düzeni tanımlaması gerekir; bu paket main dalına cache commit'i yapar.
8. assets/bfrc-logo.png adıyla resmi, tercihen şeffaf PNG logo yükleyin. Logo verilmezse %15 opak BFRC metni kullanılır. Başka bir kurum logosu otomatik alınmaz.
9. Actions → BFRC API update and publish → Run workflow → main. İş akışı testleri çalıştırır, API'den verileri alır, cache'i kaydeder, Pages'e gönderir.
10. İş tamamlanınca deployment/Pages bölümündeki gerçek HTTPS URL'yi açın. Örnek biçim: https://KULLANICI.github.io/bfrc-panel/ . Yer tutucuyu kendi URL'nizle değiştirin.

Public repository içindeki kaynak kod ve state/cache.json herkes tarafından görülebilir. Bu dosyada yalnızca ilgili sağlayıcının kamuya yeniden yayımlanmasına izin verdiği gözlemleri tutun. FRED'deki her seri aynı yeniden dağıtım hakkına sahip değildir; yeni serilerde özgün sağlayıcının kullanım şartını kontrol edin. Bloomberg terminal verisini bu yapıya otomatik aktarmayın; kamuya dağıtım için ayrı veri lisansı gerekir.

## 3. WordPress'e ekleme

1. Sayfalar → Güncel Grafikler → Düzenle.
2. Mevcut iframe kodunu ayrı yere kopyalayın; geri dönüş için saklayın.
3. Aynı HTML widget'ında veya Özel HTML bloğunda eski iframe yerine şu kodu ekleyin. Editör türüne göre panel isimleri değişebilir.

```html
<iframe
  src="https://KULLANICI.github.io/bfrc-panel/"
  title="BFRC Güncel Grafikler"
  width="100%"
  height="1300"
  style="border:0;display:block;"
  loading="lazy">
</iframe>
<p><a href="https://KULLANICI.github.io/bfrc-panel/"
  target="_blank" rel="noopener">Grafikleri tam ekran aç</a></p>
```

4. Önizleme → masaüstü ve telefon. Sonra Güncelle.
5. iframe kaydettikten sonra siliniyorsa WordPress rolü HTML kısıtlaması uyguluyor olabilir. Mevcut iframe'in bulunduğu widget'ı kullanın; kurumun WordPress yöneticisine iframe iznini kontrol ettirin. Bu durumda yalnızca sayfa düzenleme yetkisi yeterli olmayabilir.
6. Panel ayrı URL'de çalışıyor fakat WordPress içinde boşsa tarayıcı konsolunda Content-Security-Policy/frame-src ve iframe engellerini kontrol edin. WordPress güvenlik politikası github.io alanına izin vermiyorsa site yöneticisi bu alanı açmalıdır.

Sabit yükseklik bu başlangıç paketinde bilinçli tercih edilmiştir; tarayıcı iframe içinde kaydırır. Grafik sayısı artarsa yüksekliği artırın. Telefon ekranında grafiğin tamamını görmek için yatay kaydırma ve Tam ekran aç bağlantısı vardır. Otomatik iframe yüksekliği için WordPress tarafında ek JavaScript/postMessage kurulumuna ihtiyaç olur; bu temel kurulum için zorunlu değildir.

## 4. Güncelleme ve hata davranışı

Varsayılan cron: `17 0,6,9,12,15,18,21 * * *` (UTC). Türkiye saatiyle her gün 00:17, 03:17, 09:17, 12:17, 15:17, 18:17 ve 21:17 için planlanır. GitHub görevleri gecikebilir, yoğunlukta düşebilir; tam saat garantisi yoktur. Planlanmış işler varsayılan dalda çalışır. Actions → Run workflow ile ayrıca manuel yenileme yapılır.

Kod her çalıştırmada 2020'den bugüne kadar seriyi yeniden alır; geçmiş revizyonları da yakalar. Başlangıçtaki iki FRED serisi için istek yükü düşüktür. Yüzlerce seri veya çok uzun geçmişte seri bazında yayın takvimi, daha seyrek tam revizyon taraması ve artımlı yükleme eklenmelidir.

| Olay | Davranış |
|---|---|
| API başarılı | Doğrulanmış yeni geçmiş alınır, son gözlem ve kontrol saati yazılır |
| Bir API başarısız, cache var | Önceki başarılı gözlemler korunur; grafik ve panelde uyarı gösterilir |
| Bir API başarısız, cache yok | İlgili grafik gösterilmez; panel hata listesini gösterir |
| Tüm API'ler başarısız, cache var | Uyarılı eski veri paneli yayımlanır; son workflow adımı başarısız olarak işaretlenir |
| Hiç veri yok | Build başarısız; mevcut Pages yayınına dokunulmaz |
| Workflow/Pages yayını başarısız | Önceki yayımlanmış panel kalır; 24 saat sonra panel kendisi gecikme uyarısı gösterir |

Eksik veri sıfıra çevrilmez. Fark hesabı yalnızca iki serinin de gözlemi olan aynı tarihlerde yapılır. Getiriler yüzde puan olarak geldiği için fark ×100 ile baz puana dönüştürülür. Farklı tarihli iki son değer birbirinden çıkarılmaz. NaN, sonsuz ve gelecekteki tarihler reddedilir.

Günlük seriler için max_age_days=7 kullanıldı; hafta sonu/tatiller dikkate alınarak toleranslı başlangıç eşiğidir. Bu eşik bir veri açıklama takvimi değildir. Aylık serilerde yaklaşık 45–60 gün gibi uygun eşik seçilmeli ve kaynağın takvimine göre doğrulanmalıdır. Her grafikte son gözlem tarihi bulunur; bu tarih kaynak tarafından veri verilen dönem tarihidir, açıklama tarihi değildir.

Cache repository'de saklandığı için GitHub runner silinse bile son başarılı veri kaybolmaz. Repository içindeki cache güncelleme commit'leri yenileme geçmişini gösterir. `[skip ci]` ve paths-ignore tekrar tetiklenmeyi önler. GitHub public repository'lerde 60 gün repository aktivitesi olmazsa zamanlanmış workflow'ları kapatabilir. Bu cache commit'lerine rağmen aylık olarak Actions durumunu kontrol edin; kapandıysa Enable workflow ve Run workflow kullanın. Hesap bildirimlerinde failed workflow bildirimini açın.

## 5. Grafik görünümü

Her grafikte aynı ortak fonksiyon uygulanır:

- Resmi logo dosyası varsa BFRC logo görseli, opacity=0.15, veri çizgilerinin altında.
- Logo yoksa geçici BFRC yazısı, opacity=0.15.
- `Kaynak: ...` — grafiğin kendi kaynağı; tek bir genel kaynak etiketi değildir.
- `Grafik: Mehmet Faruk Bekir`.
- Son gözlem tarihi ve son başarılı API kontrol saati.
- Yakınlaştırma, tarih aralığı düğmeleri ve PNG indirme.

Logo ve kaynak/isim satırları HTML çevresinde ayrı bir overlay olarak değil, Plotly figürünün içinde bulunur; kamera simgesinden alınan PNG'de de yer alır. Plotly JavaScript dosyası yerelde yayımlanır; harici CDN gerektirmez. Resmi logo yerel PNG'den data URI olarak figüre gömülür.

Temel filigran kodu:

```python
fig.add_layout_image(
    source=logo_data_uri, xref="paper", yref="paper",
    x=.5, y=.5, xanchor="center", yanchor="middle",
    sizex=.60, sizey=.60, sizing="contain",
    opacity=.15, layer="below"
)
fig.add_annotation(
    text="Grafik: Mehmet Faruk Bekir", xref="paper", yref="paper",
    x=0, y=0, yshift=-83, xanchor="left", showarrow=False
)
```

## 6. Yerel çalıştırma

Python 3.12 önerilir. Terminali paket klasöründe açın:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m unittest discover -s tests
python build.py --demo
python -m http.server 8000 --directory demo
```

Tarayıcı: http://localhost:8000 . Demo veriler sentetiktir ve her grafikte açıkça etiketlidir. Terminalde Ctrl+C sunucuyu durdurur.

Gerçek API ile, kendi anahtarınızla:

```powershell
$env:FRED_API_KEY="KENDI_ANAHTARINIZ"
python build.py
python -m http.server 8000 --directory dist
```

Linux/macOS'ta `.venv/bin/activate` ile etkinleştirin ve `export FRED_API_KEY=...` kullanın. Anahtarı paylaşmayın veya kod dosyasına yazmayın. Yerel terminal komut geçmişinin de anahtar içerebileceğini dikkate alın; GitHub'da secret girişini tercih edin.

## 7. Yeni grafik ekleme

FRED'de serinin açıklamasını ve kullanım koşulunu okuyun. config.json → series listesine yeni id, provider, code, enabled ve max_age_days ekleyin. charts listesine aynı id'yi kullanan başlık, birim, kaynak ve label ekleyin. Commit sonrası görev yeni grafiği üretir.

Örnek, seri anlamı ve kaynak doğrulandıktan sonra:

```json
{"id":"fed-rate","provider":"fred","code":"DFF","enabled":true,"max_age_days":7}
```

```json
{"id":"fed-rate-chart","title":"Efektif federal fon oranı","unit":"%",
 "source":"Federal Reserve Bank of New York / FRED",
 "series":[{"id":"fed-rate","label":"Efektif federal fon oranı"}]}
```

Bir seri kimliği başka provider/code için kullanılırsa eski cache alınmaz. Yeni sağlayıcı için build.py'ye ayrı fetch fonksiyonu ekleyin; çıktı her zaman `[{"date":"YYYY-MM-DD","value":sayı}, ...]` olmalıdır. TÜFE yıllık değişimi gerekiyorsa mevcut yüzde serisini kullanın veya endeksin aynı ay geçen yıl gözlemiyle hesaplayın; eksik ayları satır kaydırmayla eşleştirmeyin.

## 8. Canlıya alma kontrolü

Önce Pages URL'de iki ABD grafiğini kontrol edin. Son gözlem değerlerini resmi kaynakla aynı tarihte karşılaştırın. PNG indirmede %15 filigran, veri kaynağı ve ad/soyad görünmeli. Sonra WordPress iframe önizlemesini açın. Güncellemeden sonra otomatik görevin sonraki çalışmasını ve API kontrol saatinin değişmesini kontrol edin. EVDS günlük örneği kendi hesabınızla doğrulanmadan Türkiye grafiğini açmayın.

Bu paketin hazırlanması sırasında gerçek API anahtarıyla FRED/EVDS sorgusu, GitHub Actions yayını ve gerçek WordPress iframe testi yapılmadı. Offline testler ve sentetik demo üretimi kontrol edildi. Hesap, logo ve canlı bağlantı doğrulaması kurulumun kalan adımlarıdır.

## Resmi başvuru kaynakları

- GitHub Pages ve public/free desteği: https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages
- Pages için Actions yayını: https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages
- Cron, gecikmeler ve 60 gün kuralı: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
- FRED observations API: https://fred.stlouisfed.org/docs/api/fred/series_observations.html
- FRED API anahtarı: https://fred.stlouisfed.org/docs/api/api_key.html
- Plotly görseller/filigran: https://plotly.com/python/images/
- TCMB EVDS3 duyurusu: https://www.tcmb.gov.tr/wps/wcm/connect/TR/TCMB+TR/Main+Menu/Duyurular/Basin/2026/DUY2026-03
- EVDS: https://evds3.tcmb.gov.tr/

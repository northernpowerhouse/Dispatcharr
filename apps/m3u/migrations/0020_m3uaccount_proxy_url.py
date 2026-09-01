# Generated for the per-provider proxy routing feature.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('m3u', '0019_m3uaccountprofile_exp_date'),
    ]

    operations = [
        migrations.AddField(
            model_name='m3uaccount',
            name='proxy_url',
            field=models.CharField(
                blank=True,
                help_text=(
                    "Optional outbound proxy for all requests to this provider "
                    "(M3U fetch, Xtream API, live/VOD playback). "
                    "http://[user:pass@]host:port, socks5://[user:pass@]host:port, "
                    "or socks5h://[user:pass@]host:port (resolves DNS through the "
                    "proxy — recommended for geo-restricted providers)."
                ),
                max_length=500,
                null=True,
            ),
        ),
    ]

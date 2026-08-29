async def setup(bot):
    from .cog import GoStudyChalkCog

    await bot.add_cog(GoStudyChalkCog(bot))
